#!/usr/bin/env python3
"""Copy captured frames into a Box Drive folder, from S3 (ECS) and GitHub artifacts.

ECS deployment: `aws s3 sync s3://<bucket>/events/ MIRROR`. The capture workflow also
uploads each run's frames as an artifact ("captures-<run id>", kept 90 days); every
artifact not synced yet is downloaded into MIRROR with the matching event.json.

MIRROR is a local copy outside Box with the bucket's own layout, so `aws s3 sync` only
fetches what is new. It is then copied into DEST with the camera folders numbered per
event (nearest camera = 1; a camera that shows up later gets the next number, and
numbers never change once given):

    DEST/<date>/HTF<id>_<yyyymmdd>[_<k>]/event.json
    DEST/<date>/HTF<id>_<yyyymmdd>[_<k>]/<n>_<source>_<camera>/<UTC stamp>.jpg

Event folders are named by HTF point and day; when one point has several events that
day (two high tides), they get _1, _2 … in time order (so the first one is renamed
from HTF<id>_<yyyymmdd> to ..._1 when a second event appears).

Afterwards HTF_camera_review.xlsx gets rows for any new event × camera (skipped while
the workbook is open in Excel; earlier answers are always kept).

With --news-dest, the NWS Local Storm Reports and news articles found near each event
(S3 news/, written by crossval/reports.py and crossval/news.py) are copied there too,
with a Flood_reports.html page per event and HTF_flood_reports.xlsx listing them.

Box Drive then uploads the files. Needs the GitHub CLI (`gh`) logged in.

Usage:
    python3 tools/sync_to_box.py --dest "/Users/<you>/Library/CloudStorage/Box-Box/<folder>" \\
        [--news-dest "/Users/<you>/Library/CloudStorage/Box-Box/<news folder>"]
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "Ehsankahrizi/TWL_Camera_CrossValidation"
S3_BUCKET = "bil6-twl-camera-crossval-858933856877"     # ECS deployment
STATE_FILE = ".synced_artifacts.json"
MIRROR = Path("~/Library/Application Support/TWLBoxSync/mirror").expanduser()
NEWS_MIRROR = Path("~/Library/Application Support/TWLBoxSync/news_mirror").expanduser()
NUMBERED = re.compile(r"^(\d+)_(.+)$")             # "2_windy_1651853027" → 2, "windy_1651853027"


def gh(*args, capture=True):
    r = subprocess.run(["gh", *args], capture_output=capture, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}… failed: {r.stderr.strip()[:300]}")
    return r.stdout


def list_artifacts():
    out = gh("api", "--paginate", f"repos/{REPO}/actions/artifacts?per_page=100",
             "--jq", '.artifacts[] | select(.name|startswith("captures-")) | select(.expired|not) | [.id, .name, .workflow_run.id] | @tsv')
    arts = []
    for line in out.splitlines():
        aid, name, run_id = line.split("\t")
        arts.append((int(aid), name, run_id))
    return sorted(arts)


def fetch_event_json(date, event_id):
    try:
        return gh("api", f"repos/{REPO}/contents/events/{date}/{event_id}/event.json",
                  "-H", "Accept: application/vnd.github.raw")
    except RuntimeError:
        return None


def refresh_review_sheet(dest):
    """Add rows for new events to HTF_camera_review.xlsx (never while it is open)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from make_review_sheet import refresh_if_needed
        print(f"Review workbook {refresh_if_needed(dest)}")
    except Exception as e:                       # the sync itself must not fail because of the sheet
        print(f"Review workbook not updated: {e}")


def box_event_names(event_ids):
    """{event_id: Box folder name}: '20260925T00Z_HTF1016_exceedance' → 'HTF1016_20260925', or
    'HTF1016_20260925_1', '..._2' (time order) when that point has several events that day."""
    groups = {}
    for eid in event_ids:
        m = re.match(r"^(\d{8})T\d{2}Z_(HTF\d+)_", eid)
        groups.setdefault(f"{m.group(2)}_{m.group(1)}" if m else eid, []).append(eid)
    out = {}
    for base, ids in groups.items():
        ids.sort()
        for k, eid in enumerate(ids, 1):
            out[eid] = base if len(ids) == 1 else f"{base}_{k}"
    return out


def event_id_of(ev_dir, marker="event.json"):
    try:
        return json.loads((ev_dir / marker).read_text())["event_id"]
    except (OSError, ValueError, KeyError):
        return None


def name_event_folders(dest, mirror_ids, marker="event.json"):
    """Rename Box event folders to their HTF/day names; returns {event_id: Box folder Path}.

    Box folders are found by the event_id inside `marker` (event.json, or lsr.json /
    news.json in the news folder), whatever they are named now
    (original, an earlier scheme, or a name that changed because a second event appeared).
    """
    here = {}                                           # event_id → current Box folder
    for m in (marker,) if isinstance(marker, str) else marker:
        for ev_json in dest.glob(f"*/*/{m}"):
            eid = event_id_of(ev_json.parent, m)
            if eid:
                here[eid] = ev_json.parent
    ids = set(here) | set(mirror_ids)
    want = {}
    for date in {mirror_ids.get(e) or here[e].parent.name for e in ids}:
        day = [e for e in ids if (mirror_ids.get(e) or here[e].parent.name) == date]
        want.update({e: dest / date / n for e, n in box_event_names(day).items()})
    moving = [e for e in here if here[e] != want[e]]
    for e in moving:                                    # two steps, so swapped names cannot collide
        here[e] = here[e].rename(here[e].with_name(f".renaming_{e}"))
    for e in moving:
        here[e] = here[e].rename(want[e])
    return want


def numbered_dirs(event_dir):
    """{plain camera folder name: numbered folder Path} already in a Box event folder."""
    out = {}
    for d in event_dir.iterdir() if event_dir.is_dir() else []:
        m = NUMBERED.match(d.name)
        if d.is_dir() and m:
            out[m.group(2)] = d
    return out


def number_folders(event_dir, order):
    """Give every unnumbered camera folder in a Box event folder the next free number.

    `order` lists plain folder names, nearest camera first. Already-numbered folders keep
    their number; renaming (not copying) keeps the files Box has already uploaded.
    """
    have = numbered_dirs(event_dir)
    nxt = max((int(NUMBERED.match(d.name).group(1)) for d in have.values()), default=0) + 1
    plain = [d.name for d in event_dir.iterdir() if d.is_dir() and not NUMBERED.match(d.name)]
    for name in sorted(plain, key=lambda n: (order.index(n) if n in order else len(order), n)):
        (event_dir / name).rename(event_dir / f"{nxt}_{name}")
        nxt += 1


def camera_order(event_json):
    """Plain camera folder names of an event, nearest first."""
    try:
        cams = json.loads(event_json.read_text())["cameras"]
    except (OSError, ValueError, KeyError):
        return []
    cams = sorted(cams, key=lambda c: c.get("distance_km") if c.get("distance_km") is not None else 99)
    return [f"{c['source']}_{re.sub(r'[^A-Za-z0-9._-]+', '_', str(c['id'])).strip('_')[:80]}" for c in cams]


def publish(mirror, dest):
    """Copy new/changed files from the mirror into Box, with numbered camera folders."""
    copied = 0
    sources = {ev.name: ev for ev in (p.parent for p in mirror.glob("*/*/event.json"))}
    box_dirs = name_event_folders(dest, {e: ev.parent.name for e, ev in sources.items()})
    for eid, ev_src in sorted(sources.items()):
        ev_dst = box_dirs[eid]
        ev_dst.mkdir(parents=True, exist_ok=True)
        have = numbered_dirs(ev_dst)
        for cam_src in (d for d in ev_src.iterdir() if d.is_dir()):
            if cam_src.name not in have:
                (ev_dst / cam_src.name).mkdir(exist_ok=True)
        number_folders(ev_dst, camera_order(ev_src / "event.json"))
        targets = numbered_dirs(ev_dst)
        pairs = [(ev_src / "event.json", ev_dst / "event.json")]
        for cam_src in (d for d in ev_src.iterdir() if d.is_dir()):
            pairs += [(f, targets[cam_src.name] / f.name) for f in cam_src.iterdir() if f.is_file()]
        for src, dst in pairs:
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dst)
                copied += 1
    for eid, ev_dir in box_dirs.items():                # also events only in Box (older artifact syncs)
        if eid not in sources:
            number_folders(ev_dir, camera_order(ev_dir / "event.json"))
    print(f"{copied} file(s) copied into {dest}")


PAGE = "Flood_reports.html"
WORKBOOK = "HTF_flood_reports.xlsx"
OLD_FILES = ("NWS_reports.html", "HTF_LSR_reports.xlsx")   # names used before news articles were added


def publish_news(news_mirror, news_dest, capture_mirror):
    """Copy flood evidence (S3 news/: NWS storm reports and news articles) into Box.

    Events with an NWS report or a likely/possible news article get a folder, named like
    its captures folder:
        NEWS_DEST/<date>/HTF<id>_<yyyymmdd>[_<k>]/lsr.json, news.json, Flood_reports.html
    The HTML page shows a map, every NWS report with a link to the NWS text, and the news
    articles with links. HTF_flood_reports.xlsx lists all of it, plus every event checked
    (also those with nothing found); event folder names link to the page.
    """
    lsr_files = {p.parent.name: p for p in news_mirror.glob("*/*/lsr.json")}
    news_files = {p.parent.name: p for p in news_mirror.glob("*/*/news.json")}
    if not lsr_files and not news_files:
        print("No storm-report or news files yet")
        return
    lsr = {eid: json.loads(p.read_text()) for eid, p in lsr_files.items()}
    news = {eid: drop_outside_dates(json.loads(p.read_text())) for eid, p in news_files.items()}
    # Name folders over all events of each day, so names match the captures folders.
    all_ids = {ev.name: ev.parent.name for ev in (p.parent for p in capture_mirror.glob("*/*/event.json"))}
    for files in (lsr_files, news_files):
        all_ids.update({eid: p.parent.parent.name for eid, p in files.items()})
    box_dirs = name_event_folders(news_dest, all_ids, marker=("lsr.json", "news.json"))
    copied = 0
    for eid in sorted(set(lsr) | set(news)):
        reports = (lsr.get(eid) or {}).get("reports") or []
        articles = [a for a in (news.get(eid) or {}).get("articles") or [] if a["label"] != "unlikely"]
        if not reports and not articles:
            continue
        folder = box_dirs[eid]
        folder.mkdir(parents=True, exist_ok=True)
        for files in (lsr_files, news_files):
            if eid in files:
                dst = folder / files[eid].name
                if not dst.exists() or dst.read_bytes() != files[eid].read_bytes():
                    shutil.copy2(files[eid], dst)
                    copied += 1
        html = news_page(lsr.get(eid), news.get(eid), folder.name)
        page = folder / PAGE
        if not page.exists() or page.read_text() != html:
            page.write_text(html)
        for old in OLD_FILES:
            (folder / old).unlink(missing_ok=True)
    (news_dest / OLD_FILES[1]).unlink(missing_ok=True)
    print(f"{copied} flood-evidence file(s) copied into {news_dest}")
    write_news_workbook(news_dest, lsr, news, {e: p.relative_to(news_dest) for e, p in box_dirs.items()})


def drop_outside_dates(news):
    """news.json with only the articles published within its search dates (UTC days, inclusive).

    Google News sometimes ignores after:/before: and returns unrelated items from months away;
    news.json files written before crossval/news.py filtered them still contain those."""
    s = news.get("search") or {}
    if not s.get("published_from") or not s.get("published_before"):
        return news
    lo, hi = s["published_from"], s["published_before"]
    keep = [a for a in news.get("articles") or []
            if not a.get("published") or lo <= parse_iso(a["published"]).date().isoformat() <= hi]
    return dict(news, articles=keep, n_articles=len(keep),
                n_likely=sum(a["label"] == "likely" for a in keep),
                n_possible=sum(a["label"] == "possible" for a in keep))


def news_page(lsr, news, folder_name):
    """Flood_reports.html: a plain report page with a map of the HTF point and the NWS
    reports, a table of the NWS reports (links to the NWS text and Google Maps), and a
    table of the news articles by score."""
    from html import escape as esc

    def short(t):
        return esc((t or "")[:16].replace("T", " "))

    def rel_h(h):
        if h is None:
            return ""
        return "in window" if h == 0 else f"{h:+g} h"

    base = lsr or news
    reports = (lsr or {}).get("reports") or []
    articles = (news or {}).get("articles") or []
    w = base.get("window", {})
    radius = ((lsr or {}).get("search") or {}).get("radius_km", 20)
    np_ = (news or {}).get("place") or {}
    news_where = " or ".join(x for x in (np_.get("place"), np_.get("county")) if x)
    final = all(x.get("final") for x in (lsr, news) if x)
    checked = max(x["checked_at"] for x in (lsr, news) if x)

    rows = []
    for i, r in enumerate(reports, 1):
        links = []
        if r.get("product_url"):
            links.append(f'<a href="{esc(r["product_url"])}" target="_blank">NWS text</a>')
        links.append(f'<a href="https://www.google.com/maps?q={r["lat"]},{r["lon"]}" target="_blank">map</a>')
        mag = f" ({esc(str(r['magnitude']))} {esc(r.get('unit') or '')})" if r.get("magnitude") else ""
        rows.append(f"<tr><td class=n>{i}</td><td class=nw>{short(r.get('time_local') or r['time'])}</td>"
                    f"<td class=nw>{rel_h(r['hours_from_window'])}</td><td class=r>{r['distance_km']:.1f}</td>"
                    f"<td>{esc(r['type'].title())}{mag}</td><td>{esc(r.get('city') or '')}, {esc(r.get('state') or '')}</td>"
                    f"<td>{esc(r.get('source') or '')}</td><td class=nw>{' · '.join(links)}</td></tr>"
                    f"<tr class=rem><td></td><td colspan=7>{esc(r.get('remark') or '(no remark)')}</td></tr>")
    if rows:
        nws_html = ("<table class=nws><thead><tr><th>#</th><th>Time (local)</th><th>vs. window</th><th>km</th><th>Type</th>"
                    "<th>Location</th><th>Reported by</th><th>Links</th></tr></thead><tbody>"
                    + "".join(rows) + "</tbody></table>")
    else:
        nws_html = "<p>None within the search area and time. Most minor tidal flooding is never reported to NWS.</p>"

    def art_rows(items):
        out = []
        for a in items:
            h = a.get("hours_from_window_start")
            rel = "" if h is None else f"<br><span class=why>{h:+g} h vs. window start</span>"
            out.append(f"<tr><td class=nw>{esc(a['label'])}</td><td class=r>{a['score']}</td>"
                       f"<td class=nw>{short(a.get('published_local') or a.get('published'))}{rel}</td>"
                       f"<td>{esc(a.get('source') or '')}</td>"
                       f"<td><a href=\"{esc(a['url'] or '')}\" target=\"_blank\">{esc(a['title'])}</a>"
                       f"<br><span class=why>{esc(', '.join(a['reasons']) or 'no keywords matched')}</span></td></tr>")
        return ("<table><thead><tr><th>Label</th><th>Score</th><th>Published (local)</th>"
                "<th>Outlet</th><th>Headline</th></tr></thead><tbody>" + "".join(out) + "</tbody></table>")

    shown = [a for a in articles if a["label"] != "unlikely"]
    hidden = [a for a in articles if a["label"] == "unlikely"]
    if news:
        p = news.get("place") or {}
        where = ", ".join(x for x in (p.get("place"), p.get("county"), p.get("state")) if x)
        news_html = (f'<p class=note>Google News search for {esc(where)} with flood terms, published '
                     f'{esc(news["search"]["published_from"])} to {esc(news["search"]["published_before"])}. '
                     'Labels are from headline keywords only; read an article before using it as evidence.</p>'
                     + (art_rows(shown) if shown else "<p>No likely or possible articles.</p>")
                     + (f"<details><summary>{len(hidden)} articles labelled unlikely</summary>{art_rows(hidden)}</details>" if hidden else ""))
    else:
        news_html = "<p>Not checked yet.</p>"

    pts = json.dumps([{"lat": r["lat"], "lon": r["lon"], "n": i} for i, r in enumerate(reports, 1)])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(folder_name)} flood reports</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Sans+3:ital,wght@0,400;0,600;1,400&display=swap">
<style>
 body {{ margin:0; background:#fff; color:#1b1b1b; font:16px/1.55 "Source Sans 3", "Segoe UI", Helvetica, Arial, sans-serif; }}
 main {{ max-width:1000px; margin:0 auto; padding:20px 16px 40px; }}
 h1 {{ font-size:24px; font-weight:600; margin:0 0 8px; }}
 h2 {{ font-size:19px; font-weight:600; margin:30px 0 8px; padding-bottom:4px; border-bottom:1px solid #999; }}
 dl {{ display:grid; grid-template-columns:max-content 1fr; gap:2px 16px; margin:0 0 14px; font-size:15px; }}
 dt {{ color:#555; }} dd {{ margin:0; }}
 #map {{ height:400px; border:1px solid #999; }}
 .rl {{ font-size:12px; font-weight:600; color:#1f6fd1; text-align:center; white-space:nowrap; text-shadow:0 0 3px #fff, 0 0 3px #fff, 0 0 3px #fff; }}
 .cap, .note {{ font-size:14px; color:#565c65; margin:6px 0 10px; }}
 table {{ border-collapse:collapse; width:100%; font-size:15px; line-height:1.45; font-variant-numeric:tabular-nums; }}
 th, td {{ border:1px solid #ccc; padding:6px 8px; vertical-align:top; text-align:left; }}
 th {{ background:#f0f0f0; font-weight:600; }}
 td.r {{ text-align:right; }} td.n {{ text-align:center; }} .nw {{ white-space:nowrap; }} .why {{ color:#565c65; font-size:13.5px; }}
 tr.rem td {{ border-top:none; padding-top:0; padding-bottom:8px; font-style:italic; }}
 table.nws tbody tr:not(.rem) td {{ border-bottom:none; }} table.nws tr.rem td:first-child {{ border-right:none; }}
 a {{ color:#1a0dab; }} a:visited {{ color:#609; }}
 details {{ margin-top:10px; }} summary {{ cursor:pointer; font-size:15px; margin-bottom:8px; }}
 .mk {{ background:#fff; border:1px solid #000; font:600 12px "Source Sans 3", Arial, sans-serif; width:18px; height:18px; line-height:18px; text-align:center; }}
 @media (max-width:700px) {{ table {{ display:block; overflow-x:auto; }} }}
</style></head>
<body><main>
<h1>{esc(folder_name)}</h1>
<dl>
 <dt>HTF point</dt><dd>{base['htf_id']} ({base['lat']:.4f}, {base['lon']:.4f})</dd>
 <dt>Forecast window</dt><dd>{short(w.get('start_local') or w.get('start'))} to {short(w.get('end_local') or w.get('end'))} local</dd>
 <dt>Found</dt><dd>{len(reports)} NWS report{'s' if len(reports) != 1 else ''}, {len(shown)} likely/possible news article{'s' if len(shown) != 1 else ''}</dd>
 <dt>Last checked</dt><dd>{short(checked)} UTC{'' if final else ' (not final; more may be added)'}</dd>
</dl>
<div id="map"></div>
<p class=cap>Red dot: HTF point. Blue circle: NWS search area, {radius:g} km around the HTF point; only storm reports inside it are kept. Numbered boxes: NWS reports in the table below. News articles have no circle: they are found by place name ({esc(news_where) or 'town or county'}), not by distance.</p>
<h2>NWS Local Storm Reports</h2>
{nws_html}
<h2>News articles</h2>
{news_html}
<p class=note>Sources: NWS Local Storm Reports (Iowa Environmental Mesonet archive; locations are town-level) and Google News. Data files: lsr.json, news.json.</p>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
 const htf = [{base['lat']}, {base['lon']}], pts = {pts};
 const map = L.map('map');
 // Esri tiles: OpenStreetMap's servers refuse pages opened from a file (no referrer).
 const esri = 'https://server.arcgisonline.com/ArcGIS/rest/services/';
 const street = L.tileLayer(esri + 'World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}', {{maxZoom: 19, attribution: 'Esri'}}).addTo(map);
 const sat = L.tileLayer(esri + 'World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{maxZoom: 19, attribution: 'Esri, Maxar, Earthstar Geographics'}});
 L.control.layers({{'Streets': street, 'Satellite': sat}}).addTo(map);
 L.control.scale({{imperial: false}}).addTo(map);
 map.setView(htf, 10);
 const area = L.circle(htf, {{radius: {radius} * 1000, color: '#1f6fd1', weight: 2, dashArray: '6 4', fillColor: '#1f6fd1', fillOpacity: 0.06}}).addTo(map)
   .bindTooltip('NWS search area: {radius:g} km', {{sticky: true}});
 // Label on the circle's northern edge.
 L.marker([htf[0] + {radius} / 111.32, htf[1]], {{interactive: false, icon: L.divIcon({{className: '', html: '<div class="rl">{radius:g} km NWS search radius</div>', iconSize: [170, 18], iconAnchor: [85, 20]}})}}).addTo(map);
 L.circleMarker(htf, {{radius: 6, color: '#000', weight: 1, fillColor: '#d00', fillOpacity: 1}}).addTo(map).bindTooltip('HTF {base['htf_id']}');
 const b = L.latLngBounds(area.getBounds());
 pts.forEach(p => {{ b.extend([p.lat, p.lon]);
   L.marker([p.lat, p.lon], {{icon: L.divIcon({{className: '', html: '<div class="mk">' + p.n + '</div>', iconSize: [20, 20], iconAnchor: [10, 10]}})}}).addTo(map); }});
 const fit = () => {{ map.invalidateSize(); map.fitBounds(b.pad(0.05)); }};
 fit();
 window.addEventListener('load', fit); window.addEventListener('resize', fit);
</script>
</body></html>
"""


FLOOD_ANSWERS = ["Yes", "No", "NaN", "Invalid"]
ANSWER_HEADER = "Does it report flooding here?"
NOTES_HEADER = "Notes (optional)"
KEY_HEADER = "Row key (hidden)"
# (header, width, key). Order = column order on the "All reports" sheet.
FLOOD_COLUMNS = [
    ("Event folder", 22, "folder"),
    ("Source", 17, "source"),
    ("HTF id", 7, "htf_id"),
    ("Time (local)", 20, "time"),
    ("Hours from window", 9, "hours"),
    ("Text (NWS remark or news headline)", 70, "text"),
    (ANSWER_HEADER, 14, "answer"),
    (NOTES_HEADER, 36, "notes"),
    ("News label", 9, "label"),
    ("News score", 7, "score"),
    ("NWS report type", 14, "nws_type"),
    ("Place", 26, "place"),
    ("Reported by / outlet", 20, "by"),
    ("Distance (km)", 9, "distance"),
    ("Why this news label", 36, "why"),
    ("Link", 28, "link"),
    ("Window start (local)", 20, "w_start"),
    ("Window end (local)", 20, "w_end"),
    ("Final", 6, "final"),
    (KEY_HEADER, 30, "key"),
]
FCOL = {key: i + 1 for i, (_, _, key) in enumerate(FLOOD_COLUMNS)}
FLOOD_BACKUPS = Path("~/Library/Application Support/TWLBoxSync/flood_backups").expanduser()
FLOOD_QUIET_MIN = 30          # skip the hourly rewrite if someone saved the workbook more recently


def flood_row_key(eid, source, item):
    """Stable id of one report/article row, so typed answers stay with it across rebuilds."""
    if source == "NWS storm report":
        return f"{eid}|nws|{item.get('product_id')}|{item['time']}|{item['lat']},{item['lon']}"
    return f"{eid}|news|{item.get('url') or item['title']}"


def flood_answers(path):
    """{row key: {column key: value}} for rows of an existing workbook where someone typed an
    answer or a note. Raises ValueError if the sheet has answers but cannot be matched safely."""
    if not path.exists():
        return {}
    from openpyxl import load_workbook
    wb = load_workbook(path)
    if "All reports" not in wb.sheetnames:
        return {}
    ws = wb["All reports"]
    head = {c.value: i for i, c in enumerate(ws[1])}
    if ANSWER_HEADER not in head:
        return {}                                        # older version without answer columns
    missing = [h for h in (ANSWER_HEADER, NOTES_HEADER, KEY_HEADER) if h not in head]
    if missing:
        raise ValueError(f"column header(s) changed or missing: {missing}")
    by_header = {h: key for h, _, key in FLOOD_COLUMNS}
    out = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        vals = {by_header[h]: r[i] for h, i in head.items() if h in by_header}
        if vals.get("key") and (vals.get("answer") not in (None, "") or vals.get("notes") not in (None, "")):
            out[vals["key"]] = vals
    return out


def write_news_workbook(news_dest, lsr, news, names):
    """HTF_flood_reports.xlsx.

    "All reports": one row per NWS report and per news article (column B "Source"), with two
    yellow columns to fill in by hand: a Yes/No/NaN/Invalid drop-down and notes. Typed answers
    are carried over on every rebuild (matched by a hidden row key; rows that disappear keep
    their answer at the bottom), the new file is checked before it replaces the old one, and
    the old one is backed up. "Events": one row per event checked. "Instructions", "Progress".
    """
    import tempfile
    from datetime import datetime, timezone
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation
    out = news_dest / WORKBOOK
    if (news_dest / f"~${out.name}").exists():
        print(f"{out.name} is open in Excel; not updated")
        return
    if out.exists() and datetime.now().timestamp() - out.stat().st_mtime < FLOOD_QUIET_MIN * 60 and flood_answers(out):
        print(f"{out.name} was saved less than {FLOOD_QUIET_MIN} min ago (someone may be editing); not updated")
        return
    try:
        old = flood_answers(out)
    except ValueError as e:
        print(f"{out.name} not updated: {e}")
        return
    yellow = PatternFill("solid", start_color="FFFF00")
    blue = Font(color="0563C1", underline="single")

    # ── rows ──
    rows = []
    events = []
    for eid in sorted(set(lsr) | set(news)):
        L_, N_ = lsr.get(eid) or {}, news.get(eid) or {}
        base = L_ or N_
        w = base.get("window", {})
        name = names[eid].name if eid in names else eid
        page = (names[eid] / PAGE).as_posix() if eid in names else None
        reports, arts = L_.get("reports") or [], N_.get("articles") or []
        has = bool(reports) or any(a["label"] != "unlikely" for a in arts)
        p = N_.get("place") or {}
        where = ", ".join(x for x in (p.get("place"), p.get("county"), p.get("state")) if x)
        common = {"folder": name, "page": page if has else None, "htf_id": base["htf_id"],
                  "w_start": w.get("start_local"), "w_end": w.get("end_local")}
        for r in reports:
            rows.append(dict(common, source="NWS storm report", time=r.get("time_local") or r["time"],
                             hours=r["hours_from_window"], text=r.get("remark"), nws_type=r["type"],
                             place=", ".join(x for x in (r.get("city"), r.get("state")) if x), by=r.get("source"),
                             distance=r["distance_km"], link=r.get("product_url"), final=L_.get("final"),
                             key=flood_row_key(eid, "NWS storm report", r)))
        w0, w1 = (parse_iso(w[k]) if w.get(k) else None for k in ("start", "end"))
        for a in arts:
            t = parse_iso(a["published"]) if a.get("published") else None
            h = None
            if t and w0 and w1:
                h = 0.0 if w0 <= t <= w1 else round(((t - w0) if t < w0 else (t - w1)).total_seconds() / 3600, 1)
            rows.append(dict(common, source="News article", time=a.get("published_local") or a.get("published"),
                             hours=h, text=a["title"], label=a["label"], score=a["score"], place=where,
                             by=a.get("source"), why=", ".join(a["reasons"]), link=a.get("url"), final=N_.get("final"),
                             key=flood_row_key(eid, "News article", a)))
        n_news = (N_.get("n_likely") or 0) + (N_.get("n_possible") or 0)
        events.append([name, " + ".join(x for x, ok in (("NWS storm report", reports), ("News article", n_news)) if ok)
                       or "None", base["htf_id"], base.get("kind"), w.get("start_local"), w.get("end_local"),
                       L_.get("n_reports"), L_.get("n_coastal_flood"), min((r["distance_km"] for r in reports), default=None),
                       N_.get("n_likely"), N_.get("n_possible"), N_.get("n_articles"), where,
                       max(x["checked_at"] for x in (L_, N_) if x), all(x.get("final") for x in (L_, N_) if x),
                       common["page"]])
    present = {r["key"] for r in rows}
    for k, v in old.items():                               # answered rows no longer found: keep them
        if k not in present:
            folder = (v.get("folder") or "").replace(" (no longer found)", "")
            rows.append(dict(v, folder=f"{folder} (no longer found)".strip(), page=None))
    for r in rows:
        if r["key"] in old:
            r["answer"], r["notes"] = old[r["key"]].get("answer"), old[r["key"]].get("notes")

    wb = Workbook()
    # ── Instructions ──
    ins = wb.active
    ins.title = "Instructions"
    ins.column_dimensions["A"].width = 24
    ins.column_dimensions["B"].width = 100
    lines = [
        ("HTF flood reports review", None), ("", None),
        ("Your task", "Each row on 'All reports' is one NWS storm report or one news article found near an HTF event. "
                      "Read it (click the text or the link) and decide whether it reports coastal flooding at this "
                      "HTF point during this event."),
        ("What to fill in", f"Only the YELLOW cells: '{ANSWER_HEADER}' (choose from the drop-down) and, if useful, "
                            f"'{NOTES_HEADER}'. Do not edit other columns; the sheet is rebuilt every hour and "
                            "keeps only the yellow cells."),
        ("Yes", "It reports water on roads, yards or other normally dry land near the HTF point, during or "
                "close to the event window."),
        ("No", "It is about this place and time but says there was no flooding, or only a forecast or warning "
               "that did not happen."),
        ("NaN = cannot tell", "The article cannot be opened (paywall, broken link) or does not say clearly. "
                              "Write the reason in Notes."),
        ("Invalid = not relevant", "It is about another place, another date, river or rain flooding only, or an "
                                   "unrelated topic. Write the reason in Notes."),
        ("Source (column B)", "'NWS storm report' = official National Weather Service Local Storm Report. "
                              "'News article' = found by Google News; 'News label' and 'News score' are only a "
                              "keyword guess from the headline."),
        ("Event folder", "Click it to open the event's Flood_reports.html page (map and all reports)."),
        ("Hours from window", "Negative = before the forecast window, 0 = inside it, positive = after it."),
    ]
    for i, (a, b) in enumerate(lines, 1):
        ins.cell(row=i, column=1, value=a).font = Font(bold=True, size=14 if i == 1 else 10)
        if b:
            c = ins.cell(row=i, column=2, value=b)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            ins.row_dimensions[i].height = 32
            if a in ("What to fill in",):
                c.fill = yellow

    # ── All reports ──
    ws = wb.create_sheet("All reports")
    for i, (h, wdt, _) in enumerate(FLOOD_COLUMNS, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(bold=True)
        c.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[c.column_letter].width = wdt
    ws.row_dimensions[1].height = 32
    dv = DataValidation(type="list", formula1='"' + ",".join(FLOOD_ANSWERS) + '"', allow_blank=True,
                        error="Choose Yes, No, NaN or Invalid from the list.", errorTitle=ANSWER_HEADER)
    ws.add_data_validation(dv)
    for r_i, r in enumerate(rows, 2):
        for _, _, key in FLOOD_COLUMNS:
            ws.cell(row=r_i, column=FCOL[key], value=r.get(key))
        if r.get("page"):
            ws.cell(row=r_i, column=FCOL["folder"]).hyperlink = r["page"]
            ws.cell(row=r_i, column=FCOL["folder"]).font = blue
        if r.get("link"):
            for key in ("text", "link"):
                ws.cell(row=r_i, column=FCOL[key]).hyperlink = r["link"]
                ws.cell(row=r_i, column=FCOL[key]).font = blue
        for key in ("answer", "notes"):
            ws.cell(row=r_i, column=FCOL[key]).fill = yellow
        ws.cell(row=r_i, column=FCOL["answer"]).alignment = Alignment(horizontal="center")
        dv.add(ws.cell(row=r_i, column=FCOL["answer"]))
    last = max(2, len(rows) + 1)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{ws.cell(1, len(FLOOD_COLUMNS)).column_letter}{last}"
    ws.column_dimensions[ws.cell(1, FCOL["key"]).column_letter].hidden = True
    for key in ("answer", "notes"):
        ws.cell(row=1, column=FCOL[key]).fill = yellow
    ws.cell(row=1, column=FCOL["answer"]).comment = Comment(
        "Yes = reports flooding here during the event; No = says there was none, or only a warning; "
        "NaN = cannot tell (paywall, unclear); Invalid = other place/date/topic. See Instructions.", "HTF review")

    # ── Events ──
    ev_ws = wb.create_sheet("Events")
    ev_ws.append(["Event folder", "Evidence found", "HTF id", "Kind", "Window start (local)", "Window end (local)",
                  "NWS reports", "Coastal flood reports", "Nearest report (km)", "News likely", "News possible",
                  "News total", "Searched place", "Checked at (UTC)", "Final"])
    for e in events:
        ev_ws.append(e[:-1])
        if e[-1]:
            ev_ws.cell(ev_ws.max_row, 1).hyperlink = e[-1]
            ev_ws.cell(ev_ws.max_row, 1).font = blue
    for i, wdt in enumerate([22, 30, 7, 11, 20, 20, 9, 12, 12, 9, 10, 9, 30, 21, 6], 1):
        ev_ws.column_dimensions[ev_ws.cell(1, i).column_letter].width = wdt
    for c in ev_ws[1]:
        c.font = Font(bold=True)
    ev_ws.freeze_panes = "A2"
    ev_ws.auto_filter.ref = ev_ws.dimensions

    # ── Progress ──
    pr = wb.create_sheet("Progress")
    pr.column_dimensions["A"].width = 34
    a = ws.cell(1, FCOL["answer"]).column_letter
    s = ws.cell(1, FCOL["source"]).column_letter
    rng, src = f"'All reports'!${a}$2:${a}${last}", f"'All reports'!${s}$2:${s}${last}"
    pr.append(["Review progress", "NWS storm report", "News article"])
    for label, crit in (("Rows", None), ("Yes", "Yes"), ("No", "No"), ("NaN", "NaN"), ("Invalid", "Invalid")):
        f = (lambda kind: f'=COUNTIF({src},"{kind}")') if crit is None else \
            (lambda kind, crit=crit: f'=COUNTIFS({src},"{kind}",{rng},"{crit}")')
        pr.append([label, f("NWS storm report"), f("News article")])
    pr.append(["Not answered yet", "=B2-SUM(B3:B6)", "=C2-SUM(C3:C6)"])
    for c in pr[1]:
        c.font = Font(bold=True)

    wb.active = wb.sheetnames.index("All reports")
    wb.calculation.fullCalcOnLoad = True
    with tempfile.TemporaryDirectory() as tmp:             # build outside Box, check, then replace
        new = Path(tmp) / out.name
        wb.save(new)
        now = flood_answers(new)
        lost = [k for k, v in old.items() if (now.get(k) or {}).get("answer") != v.get("answer")
                or (now.get(k) or {}).get("notes") != v.get("notes")]
        if lost:
            print(f"{out.name} not updated: {len(lost)} earlier answer(s) would change, e.g. {lost[0]}")
            return
        if out.exists() and old:
            FLOOD_BACKUPS.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out, FLOOD_BACKUPS / f"{out.stem}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.xlsx")
            for b in sorted(FLOOD_BACKUPS.glob(f"{out.stem}_*.xlsx"))[:-300]:
                b.unlink()
        shutil.copyfile(new, out)
    n_rep = sum(len(d.get("reports") or []) for d in lsr.values())
    n_art = sum(d.get("n_likely", 0) + d.get("n_possible", 0) for d in news.values())
    print(f"{out.name}: {n_rep} NWS reports, {n_art} likely/possible articles, {len(events)} events; "
          f"{len(old)} earlier answer(s) kept")


def parse_iso(s):
    from datetime import datetime, timezone
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="Box Drive folder to sync into")
    ap.add_argument("--s3-bucket", default=S3_BUCKET, help="bucket written by the ECS service ('' to skip)")
    ap.add_argument("--no-artifacts", action="store_true", help="skip GitHub Actions artifacts")
    ap.add_argument("--mirror", default=str(MIRROR), help="local copy of the bucket layout, outside Box")
    ap.add_argument("--news-dest", help="Box folder for NWS storm reports and news articles (S3 news/); skipped if not given")
    args = ap.parse_args()

    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    mirror = Path(args.mirror).expanduser()
    mirror.mkdir(parents=True, exist_ok=True)
    state_path = dest / STATE_FILE
    synced = set(json.loads(state_path.read_text())) if state_path.exists() else set()

    if args.s3_bucket:
        aws = shutil.which("aws")
        if not aws:
            raise RuntimeError("aws CLI not found on PATH")
        r = subprocess.run([aws, "s3", "sync", f"s3://{args.s3_bucket}/events/", str(mirror),
                            "--only-show-errors", "--no-progress"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"aws s3 sync failed: {r.stderr.strip()[:300]}")
        print(f"S3 s3://{args.s3_bucket}/events/ synced into {mirror}")
    if not args.no_artifacts:
        sync_artifacts(mirror, state_path, synced)
    publish(mirror, dest)
    refresh_review_sheet(dest)
    if args.news_dest and args.s3_bucket:
        news_dest = Path(args.news_dest).expanduser()
        news_dest.mkdir(parents=True, exist_ok=True)
        NEWS_MIRROR.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([shutil.which("aws"), "s3", "sync", f"s3://{args.s3_bucket}/news/", str(NEWS_MIRROR),
                            "--only-show-errors", "--no-progress"], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Storm reports not synced: {r.stderr.strip()[:300]}")
        else:
            publish_news(NEWS_MIRROR, news_dest, mirror)


def sync_artifacts(mirror, state_path, synced):
    todo = [a for a in list_artifacts() if a[1] not in synced]
    print(f"{len(todo)} new artifact(s) to sync into {mirror}")
    events = set()
    for aid, name, run_id in todo:
        with tempfile.TemporaryDirectory() as tmp:
            gh("run", "download", run_id, "-R", REPO, "-n", name, "-D", tmp)
            n = 0
            for f in Path(tmp).rglob("*.jpg"):
                rel = f.relative_to(tmp)                      # <date>/<event_id>/<camera>/<stamp>.jpg
                target = mirror / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f.read_bytes())
                events.add(rel.parts[:2])
                n += 1
        synced.add(name)
        state_path.write_text(json.dumps(sorted(synced), indent=1))
        print(f"  {name}: {n} frames")

    # Refresh event.json for every event touched (captures list and labels change over time).
    for date, event_id in sorted(events):
        text = fetch_event_json(date, event_id)
        if text:
            (mirror / date / event_id / "event.json").write_text(text)
    print(f"Updated {len(events)} event folder(s)")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(str(e))
