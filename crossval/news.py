"""Local news articles about flooding near each event (Google News RSS), scored by keywords.

For each event the HTF point is placed in a town (or, failing that, a county) with the
US Census geocoder. Google News is then searched for that place + its state + flood
words, for articles published from the day before the window to two days after it.
Each article is scored from its title (no AI, no API key):

    + place or county named, coastal-flood words (king tide, tidal, storm surge, overwash ...),
      street-flooding words (flooded streets, road closed ...), words reporting what happened
    - another state named, warning/forecast wording, rain/river-flood words, unrelated topics

and labelled "likely" (score >= LIKELY), "possible" (>= POSSIBLE) or "unlikely". The
labels only rank articles for a person to read; they are not a verdict. Results go to

    news/<date>/<event_id>/news.json        (S3 and the local state copy)

Like the storm reports, an event is re-checked every planning cycle until
REPORT_FINAL_AFTER_H after its window, because articles keep appearing for a day or two.

Usage:
    python -m crossval.news           # events in the schedule (the ECS runner does this)
    python -m crossval.news --all     # every event.json in S3 (one-off backfill)
"""

import argparse
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime

from . import config, util
from .util import http_get, http_json, iso, local_iso, parse_time, read_json, utcnow, write_json

GNEWS = "https://news.google.com/rss/search"
CENSUS = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
util.HOST_INTERVAL.update({"news.google.com": 3.0, "geocoding.geo.census.gov": 0.5})

LIKELY, POSSIBLE = 5, 3
FLOOD_QUERY = ('(flooding OR flooded OR flood OR "king tide" OR "high tide" OR "tidal flooding" '
               'OR "coastal flooding" OR "storm surge" OR overwash)')
COASTAL = r"king tides?|high tides?|tidal|coastal flood|storm surge|surge|overwash|nuisance flood|sunny[- ]day flood|nor.?easter|tide"
STREET = r"flooded (?:streets?|roads?|roadways?|neighborhoods?|homes|cars|parking)|streets? flood|roads? (?:closed|flood)|road closures?|water (?:on|over|covers?|covering) (?:the )?(?:roads?|streets?)|impassable|underwater|inundat"
FLOOD = r"flood"
RAIN = r"flash flood|rainfall|inches of rain|heavy rain|river flood|river crest|dam\b|levee"
OBSERVED = r"\bfloods\b|flooded|underwater|inundated|swamp|rescue|reported|damage|stranded|submerged|washed|aftermath|cleanup|clean-up"
FORECAST = r"warning|watch\b|advisory|brace|braces|expected|could|possible|threat|prepares?|forecast|ahead of|what to know|on alert"
OFF_TOPIC = r"documentary|insurance|fema map|flood map|lawsuit|obituary|football|basketball|baseball|concert|festival|recipe"
STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
          "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
          "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
          "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
          "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
          "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
          "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
          "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
          "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
          "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico"}
_places = {}                            # (lat, lon) -> place dict; the runner process stays up for days


def news_path(ev):
    return config.STATE_ROOT / "news" / ev["window"]["start"][:10] / ev["event_id"] / "news.json"


def place_of(lat, lon):
    """{"place", "county", "state", "state_name"} from the Census geocoder, or None."""
    key = (round(lat, 4), round(lon, 4))
    if key not in _places:
        j = http_json(CENSUS, params={"x": lon, "y": lat, "benchmark": "Public_AR_Current", "vintage": "Current_Current",
                                      "format": "json", "layers": "Incorporated Places,Census Designated Places,County Subdivisions,Counties,States"})
        g = ((j or {}).get("result") or {}).get("geographies")
        if g is None:
            return None                                   # service down: try again next cycle
        first = lambda k: (g.get(k) or [{}])[0]
        sub = first("County Subdivisions").get("BASENAME")
        st = first("States").get("STUSAB")
        _places[key] = {
            # Incorporated place, else CDP, else a New England town (county subdivision).
            "place": first("Incorporated Places").get("BASENAME") or first("Census Designated Places").get("BASENAME")
                     or (sub if st in ("ME", "NH", "MA", "RI", "CT") else None),
            "county": first("Counties").get("NAME"),
            "state": st, "state_name": STATES.get(st, st)}
    return _places[key]


def search(query, start, end):
    """Google News RSS items published start..end (dates, UTC), or None if unavailable."""
    q = f"{query} after:{start:%Y-%m-%d} before:{end:%Y-%m-%d}"
    r = http_get(GNEWS, params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=30)
    if r is None or not r.ok:
        return None
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return None
    out = []
    for it in root.iter("item"):
        src = it.find("source")
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except (TypeError, ValueError):
            pub = None
        title = (it.findtext("title") or "").strip()
        name = src.text.strip() if src is not None and src.text else ""
        if name and title.endswith(f" - {name}"):
            title = title[: -len(name) - 3]
        out.append({"title": title, "source": name, "source_url": src.get("url") if src is not None else None,
                    "published": iso(pub) if pub else None, "url": it.findtext("link")})
    return out


def score(title, p):
    """(score, reasons) for one headline."""
    t, why, s = title.lower(), [], 0
    names = [n for n in (p.get("place"), (p.get("county") or "").replace(" County", "").replace(" Parish", "")) if n]
    if any(n.lower() in t for n in names):
        s += 3; why.append("place named")
    if p.get("state_name") and p["state_name"].lower() in t:
        s += 1; why.append("state named")
    rest = t.replace((p.get("county") or "").lower(), "") if p.get("county") else t   # "Washington County, RI"
    others = [n for a, n in STATES.items() if a != p.get("state") and re.search(rf"\b{re.escape(n.lower())}\b", rest)]
    if p.get("state") == "WA":                           # "Washington" alone is also D.C. / a county name
        others = [o for o in others if o != "District of Columbia"]
    if others and not (p.get("state_name") or "").lower() in t:
        s -= 4; why.append(f"other state ({others[0]})")
    if re.search(COASTAL, t):
        s += 3; why.append("coastal/tidal words")
    if re.search(STREET, t):
        s += 2; why.append("street flooding words")
    if re.search(FLOOD, t):
        s += 1; why.append("flood")
    if re.search(OBSERVED, t):
        s += 1; why.append("reports flooding that happened")
    if re.search(FORECAST, t):
        s -= 1; why.append("forecast/warning wording")
    if re.search(RAIN, t):
        s -= 2; why.append("rain/river flood words")
    if re.search(OFF_TOPIC, t):
        s -= 3; why.append("off-topic words")
    return s, why


def check_event(ev, now):
    """news.json content for one event, or None if a service was unavailable."""
    p = place_of(ev["lat"], ev["lon"])
    if p is None:
        return None
    w0, w1 = parse_time(ev["window"]["start"]), parse_time(ev["window"]["end"])
    start, end = (w0 - timedelta(days=1)).date(), (w1 + timedelta(days=2)).date()
    queries = []
    if p.get("place"):
        queries.append(f'"{p["place"]}" "{p["state_name"]}" {FLOOD_QUERY}')
    if p.get("county"):
        queries.append(f'"{p["county"]}" "{p["state_name"]}" {FLOOD_QUERY}')
    articles = {}
    for q in queries:
        items = search(q, start, end)
        if items is None:
            return None
        for a in items:
            articles.setdefault(a["title"].lower(), a)
    tz = ev.get("time_zone")
    out, n_outside = [], 0
    for a in articles.values():
        s, why = score(a["title"], p)
        pub = parse_time(a["published"]) if a["published"] else None
        if pub and not start <= pub.date() <= end:
            # Google News sometimes ignores after:/before: and returns months-old or newer
            # items from the area; keep only what was published in the searched dates.
            n_outside += 1
            continue
        out.append({**a,
                    "published_local": local_iso(pub, tz) if pub else None,
                    "hours_from_window_start": round((pub - w0).total_seconds() / 3600, 1) if pub else None,
                    "score": s, "label": "likely" if s >= LIKELY else "possible" if s >= POSSIBLE else "unlikely",
                    "reasons": why})
    out.sort(key=lambda a: (-a["score"], a["published"] or ""))
    return {
        "event_id": ev["event_id"], "htf_id": ev["htf_id"], "kind": ev["kind"],
        "lat": ev["lat"], "lon": ev["lon"], "time_zone": tz, "place": p,
        "window": {k: ev["window"][k] for k in ("start", "end", "start_local", "end_local") if k in ev["window"]},
        "search": {"source": "Google News RSS", "queries": queries, "published_from": str(start),
                   "published_before": str(end), "scoring": "keywords in the headline; see crossval/news.py",
                   "n_dropped_outside_dates": n_outside},
        "checked_at": iso(now),
        "final": now >= w1 + timedelta(hours=config.REPORT_FINAL_AFTER_H),
        "n_articles": len(out),
        "n_likely": sum(a["label"] == "likely" for a in out),
        "n_possible": sum(a["label"] == "possible" for a in out),
        "articles": out,
    }


def check(events, now, force=False):
    """Write news.json for every event that is due; returns (events checked, events with likely articles)."""
    n = hits = 0
    for ev in events:
        last = parse_time(ev["window"]["end"]) + timedelta(hours=config.REPORT_FINAL_AFTER_H + 6)
        if parse_time(ev["window"]["start"]) > now or (now > last and not force):
            continue
        res = check_event(ev, now)
        if res is None:
            print(f"    ! news for {ev['event_id']}: Census or Google News unavailable; will retry next cycle")
            continue
        write_json(news_path(ev), res)
        n += 1
        hits += res["n_likely"] > 0
    return n, hits


def main():
    now = utcnow()
    schedule = read_json(config.SCHEDULE_PATH, {"events": []})
    events = [ev for ev in (read_json(config.STATE_ROOT / x["path"]) for x in schedule["events"]) if ev]
    n, hits = check(events, now)
    print(f"News check: {n} events checked, {hits} with likely flood articles")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="check every event in S3 (backfill)")
    if ap.parse_args().all:
        from .reports import backfill_all
        backfill_all(check, "News")
    else:
        main()
