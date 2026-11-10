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
    news = {eid: json.loads(p.read_text()) for eid, p in news_files.items()}
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
<style>
 body {{ margin:0; background:#fff; color:#111; font:14px/1.45 Georgia, "Times New Roman", serif; }}
 main {{ max-width:1000px; margin:0 auto; padding:20px 16px 40px; }}
 h1 {{ font-size:20px; font-weight:normal; margin:0 0 6px; }}
 h2 {{ font-size:15px; margin:26px 0 6px; padding-bottom:3px; border-bottom:1px solid #999; }}
 dl {{ display:grid; grid-template-columns:max-content 1fr; gap:1px 14px; margin:0 0 12px; font-size:13px; }}
 dt {{ color:#555; }} dd {{ margin:0; }}
 #map {{ height:340px; border:1px solid #999; }}
 .cap, .note {{ font-size:12px; color:#555; margin:4px 0 8px; }}
 table {{ border-collapse:collapse; width:100%; font:12.5px/1.35 Arial, Helvetica, sans-serif; }}
 th, td {{ border:1px solid #ccc; padding:4px 6px; vertical-align:top; text-align:left; }}
 th {{ background:#f2f2f2; font-weight:bold; }}
 td.r {{ text-align:right; }} td.n {{ text-align:center; }} .nw {{ white-space:nowrap; }} .why {{ color:#666; font-size:11.5px; }}
 tr.rem td {{ border-top:none; padding-top:0; padding-bottom:8px; font-style:italic; }}
 table.nws tbody tr:not(.rem) td {{ border-bottom:none; }} table.nws tr.rem td:first-child {{ border-right:none; }}
 a {{ color:#1a0dab; }} a:visited {{ color:#609; }}
 details {{ margin-top:8px; }} summary {{ cursor:pointer; font-size:13px; margin-bottom:6px; }}
 .mk {{ background:#fff; border:1px solid #000; font:bold 11px Arial, sans-serif; width:16px; height:16px; line-height:16px; text-align:center; }}
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
<p class=cap>Red dot: HTF point. Circle: {radius:g} km NWS search radius. Numbered boxes: NWS reports in the table below.</p>
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
 L.circle(htf, {{radius: {radius} * 1000, color: '#333', weight: 1, dashArray: '4 4', fill: false}}).addTo(map);
 L.circleMarker(htf, {{radius: 6, color: '#000', weight: 1, fillColor: '#d00', fillOpacity: 1}}).addTo(map).bindTooltip('HTF {base['htf_id']}');
 const b = [htf];
 pts.forEach(p => {{ b.push([p.lat, p.lon]);
   L.marker([p.lat, p.lon], {{icon: L.divIcon({{className: '', html: '<div class="mk">' + p.n + '</div>', iconSize: [18, 18], iconAnchor: [9, 9]}})}}).addTo(map); }});
 const fit = () => {{ map.invalidateSize(); if (pts.length) map.fitBounds(L.latLngBounds(b).pad(0.15), {{maxZoom: 13}}); else map.setView(htf, 11); }};
 map.setView(htf, 11); fit();
 window.addEventListener('load', fit); window.addEventListener('resize', fit);
</script>
</body></html>
"""


def write_news_workbook(news_dest, lsr, news, names):
    """HTF_flood_reports.xlsx: sheet "All reports" has one row per NWS report and per news
    article (column B "Source" tells which); sheet "Events" has one row per event checked."""
    out = news_dest / WORKBOOK
    if (news_dest / f"~${out.name}").exists():
        print(f"{out.name} is open in Excel; not updated")
        return
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "All reports"
    ws.append(["Event folder", "Source", "HTF id", "Window start (local)", "Window end (local)", "Time (local)",
               "Hours from window", "News label", "News score", "NWS report type", "Text (NWS remark or news headline)",
               "Place", "Reported by / outlet", "Distance (km)", "Why this news label", "Link", "Final"])
    ev_ws = wb.create_sheet("Events")
    ev_ws.append(["Event folder", "Evidence found", "HTF id", "Kind", "Window start (local)", "Window end (local)",
                  "NWS reports", "Coastal flood reports", "Nearest report (km)", "News likely", "News possible",
                  "News total", "Searched place", "Checked at (UTC)", "Final"])

    def page_link(cell, eid, has):
        rel = names.get(eid)
        if rel and has:
            cell.hyperlink = (rel / PAGE).as_posix()

    def link(url):
        if url:
            ws.cell(ws.max_row, 16).hyperlink = url
            ws.cell(ws.max_row, 11).hyperlink = url

    for eid in sorted(set(lsr) | set(news)):
        L_, N_ = lsr.get(eid) or {}, news.get(eid) or {}
        base = L_ or N_
        w = base.get("window", {})
        name = names[eid].name if eid in names else eid
        reports, arts = L_.get("reports") or [], N_.get("articles") or []
        has = bool(reports) or any(a["label"] != "unlikely" for a in arts)
        p = N_.get("place") or {}
        where = ", ".join(x for x in (p.get("place"), p.get("county"), p.get("state")) if x)
        w0, w1 = (parse_iso(w[k]) if w.get(k) else None for k in ("start", "end"))
        for r in reports:
            ws.append([name, "NWS storm report", base["htf_id"], w.get("start_local"), w.get("end_local"),
                       r.get("time_local") or r["time"], r["hours_from_window"], None, None, r["type"],
                       r.get("remark"), ", ".join(x for x in (r.get("city"), r.get("state")) if x), r.get("source"),
                       r["distance_km"], None, r.get("product_url"), L_.get("final")])
            link(r.get("product_url"))
            page_link(ws.cell(ws.max_row, 1), eid, has)
        for a in arts:
            t = parse_iso(a["published"]) if a.get("published") else None
            h = None
            if t and w0 and w1:
                h = 0.0 if w0 <= t <= w1 else round(((t - w0) if t < w0 else (t - w1)).total_seconds() / 3600, 1)
            ws.append([name, "News article", base["htf_id"], w.get("start_local"), w.get("end_local"),
                       a.get("published_local") or a.get("published"), h, a["label"], a["score"], None, a["title"],
                       where, a.get("source"), None, ", ".join(a["reasons"]), a.get("url"), N_.get("final")])
            link(a.get("url"))
            page_link(ws.cell(ws.max_row, 1), eid, has)
        n_news = (N_.get("n_likely") or 0) + (N_.get("n_possible") or 0)
        evidence = " + ".join(x for x, ok in (("NWS storm report", reports), ("News article", n_news)) if ok) or "None"
        ev_ws.append([name, evidence, base["htf_id"], base.get("kind"), w.get("start_local"), w.get("end_local"),
                      L_.get("n_reports"), L_.get("n_coastal_flood"),
                      min((r["distance_km"] for r in reports), default=None),
                      N_.get("n_likely"), N_.get("n_possible"), N_.get("n_articles"), where,
                      max(x["checked_at"] for x in (L_, N_) if x), all(x.get("final") for x in (L_, N_) if x)])
        page_link(ev_ws.cell(ev_ws.max_row, 1), eid, has)
    for sheet, widths in ((ws, [24, 18, 8, 26, 26, 26, 10, 10, 8, 15, 80, 28, 22, 10, 40, 30, 7]),
                          (ev_ws, [24, 30, 8, 11, 26, 26, 9, 12, 12, 9, 10, 9, 30, 21, 7])):
        for i, wdt in enumerate(widths, 1):
            sheet.column_dimensions[sheet.cell(1, i).column_letter].width = wdt
        for c in sheet[1]:
            c.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
    tmp = news_dest / f".{out.name}.tmp"
    wb.save(tmp)
    tmp.replace(out)
    n_rep = sum(len(d.get("reports") or []) for d in lsr.values())
    n_art = sum(d.get("n_likely", 0) + d.get("n_possible", 0) for d in news.values())
    print(f"{out.name}: {n_rep} NWS reports, {n_art} likely/possible articles, {len(set(lsr) | set(news))} events")


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
