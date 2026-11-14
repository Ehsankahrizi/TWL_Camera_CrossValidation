"""NWS Local Storm Reports (LSR) near each event: an independent check of the forecast.

When NWS offices learn of flooding (spotters, emergency managers, broadcast media, tide
gauges) they issue Local Storm Reports with a time, a location and a remark such as
"4-6 inches of water across Collins Ave during high tide". The Iowa Environmental
Mesonet (IEM) archives every LSR as GeoJSON, free and without a key.

For each event in the schedule, the flood reports (COASTAL FLOOD, FLOOD, FLASH FLOOD)
within REPORT_RADIUS_KM of the HTF point and between REPORT_BEFORE_H before the window
and REPORT_AFTER_H after it are written to

    news/<date>/<event_id>/lsr.json        (S3 and the local state copy)

LSRs are sometimes issued hours or a day late, so an event is re-checked every planning
cycle until REPORT_FINAL_AFTER_H after its window ends; "final" is then true. An empty
"reports" list is a real answer only once final is true. No report does not prove there
was no flooding: most minor tidal flooding is never reported.

Usage:
    python -m crossval.reports            # events in the schedule (the ECS runner does this)
    python -m crossval.reports --all      # every event.json in S3 (one-off backfill)
"""

import argparse
from datetime import timedelta

from . import config
from .util import dist_km, event_date, http_json, iso, local_iso, parse_time, read_json, utcnow, write_json

IEM_LSR = "https://mesonet.agron.iastate.edu/geojson/lsr.geojson"
FLOOD_TYPES = {"COASTAL FLOOD", "FLOOD", "FLASH FLOOD"}
MAX_SPAN = timedelta(days=4)            # IEM returns at most 10,000 reports (all types) per request


def report_path(ev):
    return config.STATE_ROOT / "news" / event_date(ev) / ev["event_id"] / "lsr.json"


def search_span(ev):
    w = ev["window"]
    return (parse_time(w["start"]) - timedelta(hours=config.REPORT_BEFORE_H),
            parse_time(w["end"]) + timedelta(hours=config.REPORT_AFTER_H))


def fetch_lsrs(start, end):
    """Flood LSRs issued for start..end (UTC), or None if IEM could not be read."""
    out, t = [], start
    while t < end:
        t2 = min(end, t + MAX_SPAN)
        j = http_json(IEM_LSR, params={"sts": f"{t:%Y%m%d%H%M}", "ets": f"{t2:%Y%m%d%H%M}"}, timeout=90)
        if not j or "features" not in j:
            return None
        if len(j["features"]) >= 10000:
            print(f"    ! IEM returned the 10,000-report cap for {iso(t)}..{iso(t2)}; some reports may be missing")
        out += [f["properties"] for f in j["features"] if (f.get("properties") or {}).get("typetext") in FLOOD_TYPES]
        t = t2
    return out


def hours_from_window(ev, t):
    """Negative before the window starts, 0 inside it, positive after it ends."""
    s, e = parse_time(ev["window"]["start"]), parse_time(ev["window"]["end"])
    if t < s:
        return round((t - s).total_seconds() / 3600, 2)
    if t > e:
        return round((t - e).total_seconds() / 3600, 2)
    return 0.0


def match(ev, lsrs, now):
    s, e = search_span(ev)
    tz = ev.get("time_zone")
    found = []
    for p in lsrs:
        if p.get("lat") is None or p.get("lon") is None or not p.get("valid"):
            continue
        t = parse_time(p["valid"])
        d = dist_km(ev["lat"], ev["lon"], p["lat"], p["lon"])
        if s <= t <= e and d <= config.REPORT_RADIUS_KM:
            found.append({
                "time": iso(t), "time_local": local_iso(t, tz),
                "hours_from_window": hours_from_window(ev, t),
                "distance_km": round(d, 2),
                "type": p["typetext"],
                "magnitude": p.get("magnitude") or None, "unit": p.get("unit") or None,
                "city": p.get("city"), "county": p.get("county"), "state": p.get("state") or p.get("st"),
                "wfo": p.get("wfo"), "source": p.get("source"), "remark": p.get("remark"),
                "lat": p["lat"], "lon": p["lon"],
                "product_id": p.get("product_id"),
                "product_url": f"https://mesonet.agron.iastate.edu/p.php?pid={p['product_id']}" if p.get("product_id") else None,
            })
    found.sort(key=lambda r: (r["time"], r["distance_km"]))
    final = now >= parse_time(ev["window"]["end"]) + timedelta(hours=config.REPORT_FINAL_AFTER_H)
    return {
        "event_id": ev["event_id"], "htf_id": ev["htf_id"], "kind": ev["kind"],
        "lat": ev["lat"], "lon": ev["lon"], "time_zone": tz,
        "window": {k: ev["window"][k] for k in ("start", "end", "start_local", "end_local") if k in ev["window"]},
        "search": {"source": "NWS Local Storm Reports via IEM", "api": IEM_LSR,
                   "types": sorted(FLOOD_TYPES), "radius_km": config.REPORT_RADIUS_KM,
                   "from": iso(s), "to": iso(e)},
        "checked_at": iso(now),
        "final": final,
        "n_reports": len(found),
        "n_coastal_flood": sum(r["type"] == "COASTAL FLOOD" for r in found),
        "reports": found,
    }


def check(events, now, force=False):
    """Write lsr.json for every event that is due; returns (events checked, events with reports)."""
    due = []
    for ev in events:
        s, e = search_span(ev)
        last = parse_time(ev["window"]["end"]) + timedelta(hours=config.REPORT_FINAL_AFTER_H)
        if s <= now and (force or now <= last + timedelta(hours=6)):     # +6 h: one planning cycle of slack
            due.append(ev)
    if not due:
        return 0, 0
    start = min(search_span(ev)[0] for ev in due)
    end = min(now, max(search_span(ev)[1] for ev in due))
    lsrs = fetch_lsrs(start, end)
    if lsrs is None:
        print("    ! IEM LSR service unavailable; will retry next cycle")
        return 0, 0
    hits = 0
    for ev in due:
        res = match(ev, lsrs, now)
        write_json(report_path(ev), res)
        hits += res["n_reports"] > 0
    return len(due), hits


def main():
    now = utcnow()
    schedule = read_json(config.SCHEDULE_PATH, {"events": []})
    events = [ev for ev in (read_json(config.STATE_ROOT / x["path"]) for x in schedule["events"]) if ev]
    n, hits = check(events, now)
    print(f"LSR check: {n} events checked, {hits} with flood reports nearby")


def recheck_recent():
    """Weekly: re-check every event of the last REPORT_RECHECK_DAYS days, also the ones already
    final, so reports that NWS adds days or weeks late still reach lsr.json (and the workbook)."""
    since = (utcnow() - timedelta(days=config.REPORT_RECHECK_DAYS)).date().isoformat()
    backfill_all(label=f"Weekly re-check since {since}", since=since)


def backfill_all(check_fn=None, label="Backfill", since=None):
    """Run check_fn (default: the LSR check) on every event.json in S3 (or only those of
    event dates >= since, "YYYY-MM-DD"), a day at a time."""
    check_fn = check_fn or check
    from . import store, util
    if not config.S3_BUCKET:
        raise SystemExit("S3_BUCKET is not set")
    util.WRITTEN.clear()
    keys = [o["Key"] for page in store.s3().get_paginator("list_objects_v2").paginate(Bucket=config.S3_BUCKET, Prefix="events/")
            for o in page.get("Contents", []) if o["Key"].endswith("/event.json")]
    by_date = {}
    for k in keys:
        if since is None or k.split("/")[1] >= since:
            by_date.setdefault(k.split("/")[1], []).append(k)
    now, total, hits = utcnow(), 0, 0
    for date in sorted(by_date):
        events = []
        for k in by_date[date]:
            store._download(k)
            events.append(read_json(config.STATE_ROOT / k))
        n, h = check_fn([e for e in events if e], now, force=True)
        total, hits = total + n, hits + h
        print(f"  {date}: {n} events, {h} with findings", flush=True)
    n = store.push_state()
    print(f"{label}: {total} events checked, {hits} with findings; uploaded {n} files")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="check every event in S3 (backfill)")
    ap.add_argument("--recent", action="store_true", help=f"check events of the last {config.REPORT_RECHECK_DAYS} days (the weekly re-check)")
    a = ap.parse_args()
    if a.all:
        backfill_all()
    elif a.recent:
        recheck_recent()
    else:
        main()
