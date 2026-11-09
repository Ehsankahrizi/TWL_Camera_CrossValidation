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

With --news-dest, the NWS Local Storm Reports found near each event (S3 news/, written
by crossval/reports.py) are copied there too, with HTF_LSR_reports.xlsx listing them.

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

    Box folders are found by the event_id inside `marker` (event.json, or lsr.json in the
    news folder), whatever they are named now
    (original, an earlier scheme, or a name that changed because a second event appeared).
    """
    here = {}                                           # event_id → current Box folder
    for ev_json in dest.glob(f"*/*/{marker}"):
        eid = event_id_of(ev_json.parent, marker)
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


def publish_news(news_mirror, news_dest, capture_mirror):
    """Copy NWS storm-report files (S3 news/) into the Box news folder.

    Only events with at least one report get a folder, named like its captures folder:
        NEWS_DEST/<date>/HTF<id>_<yyyymmdd>[_<k>]/lsr.json
    NWS_reports.html next to it shows a map and every report with links to the NWS text.
    HTF_LSR_reports.xlsx lists every report, and every event checked (also those with none);
    the event folder names link to that page.
    """
    files = {p.parent.name: p for p in news_mirror.glob("*/*/lsr.json")}
    if not files:
        print("No storm-report files yet")
        return
    data = {eid: json.loads(p.read_text()) for eid, p in files.items()}
    # Name folders over all events of each day, so names match the captures folders.
    all_ids = {ev.name: ev.parent.name for ev in (p.parent for p in capture_mirror.glob("*/*/event.json"))}
    all_ids.update({eid: p.parent.parent.name for eid, p in files.items()})
    box_dirs = name_event_folders(news_dest, all_ids, marker="lsr.json")
    copied = 0
    for eid, d in data.items():
        if not d.get("reports"):
            continue
        dst = box_dirs[eid] / "lsr.json"
        page = box_dirs[eid] / "NWS_reports.html"
        html = news_page(d, box_dirs[eid].name)
        if not dst.exists() or dst.read_bytes() != files[eid].read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(files[eid], dst)
            copied += 1
        if not page.exists() or page.read_text() != html:
            page.write_text(html)
    print(f"{copied} storm-report file(s) copied into {news_dest}")
    write_news_workbook(news_dest, data, {e: p.relative_to(news_dest) for e, p in box_dirs.items()})


def news_page(d, folder_name):
    """NWS_reports.html: a map of the HTF point and the reports, and one card per report
    with its remark and links to the official NWS product and Google Maps."""
    from html import escape as esc

    def when(r):
        h = r["hours_from_window"]
        return "during the forecast window" if h == 0 else f"{abs(h):g} h {'before' if h < 0 else 'after'} the window"

    w = d.get("window", {})
    cards = []
    for i, r in enumerate(d["reports"], 1):
        mag = f" · {esc(str(r['magnitude']))} {esc(r.get('unit') or '')}" if r.get("magnitude") else ""
        links = [f'<a class="btn" href="{esc(r["product_url"])}" target="_blank">Open official NWS report</a>'] if r.get("product_url") else []
        links.append(f'<a class="btn ghost" href="https://www.google.com/maps?q={r["lat"]},{r["lon"]}" target="_blank">Google Maps</a>')
        cards.append(f"""<article>
  <div class="num">{i}</div>
  <div>
    <h3>{esc(r['type'].title())} · {esc(r.get('city') or '')}, {esc(r.get('state') or '')}</h3>
    <p class="meta">{esc((r.get('time_local') or r['time'])[:16].replace('T', ' '))} local · {when(r)} · {r['distance_km']:g} km from the HTF point · reported by {esc(r.get('source') or 'unknown')}{mag}</p>
    <blockquote>{esc(r.get('remark') or '(no remark)')}</blockquote>
    <p>{' '.join(links)}</p>
  </div>
</article>""")
    pts = json.dumps([{"lat": r["lat"], "lon": r["lon"], "n": i, "t": r["type"].title(), "c": r.get("city") or ""}
                      for i, r in enumerate(d["reports"], 1)])
    radius = d.get("search", {}).get("radius_km", 20)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NWS reports · {esc(folder_name)}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
 :root {{ --bg:#f7f7f5; --card:#fff; --ink:#1d1d1b; --muted:#6b6b66; --line:#e2e2dc; --accent:#0b5cad; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --bg:#161615; --card:#21211f; --ink:#ececea; --muted:#a3a39c; --line:#34342f; --accent:#6aa8ec; }} }}
 body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system, system-ui, sans-serif; }}
 main {{ max-width:900px; margin:0 auto; padding:24px 16px 48px; }}
 h1 {{ font-size:22px; margin:0 0 4px; }} h3 {{ font-size:16px; margin:0 0 2px; }}
 .meta, .sub {{ color:var(--muted); font-size:13px; margin:0 0 8px; }}
 #map {{ height:360px; border-radius:10px; border:1px solid var(--line); margin:16px 0 20px; }}
 article {{ display:flex; gap:14px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:12px; }}
 .num {{ flex:none; width:28px; height:28px; border-radius:50%; background:var(--accent); color:#fff; display:grid; place-items:center; font-weight:600; font-size:13px; }}
 blockquote {{ margin:8px 0 10px; padding:8px 12px; border-left:3px solid var(--accent); background:var(--bg); border-radius:4px; }}
 .btn {{ display:inline-block; padding:6px 12px; border-radius:6px; background:var(--accent); color:#fff; text-decoration:none; font-size:13px; margin:0 6px 6px 0; }}
 .btn.ghost {{ background:none; color:var(--accent); border:1px solid var(--accent); }}
 .note {{ color:var(--muted); font-size:13px; margin-top:20px; }}
</style></head>
<body><main>
<h1>{esc(folder_name)}: {len(d['reports'])} NWS flood report{'s' if len(d['reports']) != 1 else ''}</h1>
<p class="sub">HTF point {d['htf_id']} ({d['lat']:.4f}, {d['lon']:.4f}) · forecast window {esc((w.get('start_local') or w.get('start', ''))[:16].replace('T', ' '))} → {esc((w.get('end_local') or w.get('end', ''))[:16].replace('T', ' '))} local · reports within {radius:g} km, checked {esc(d['checked_at'][:16].replace('T', ' '))} UTC{'' if d['final'] else ' (not final yet: late reports may still be added)'}</p>
<div id="map"></div>
{''.join(cards)}
<p class="note">Source: NWS Local Storm Reports, archived by the Iowa Environmental Mesonet. Report locations are town-level, not exact streets. The same data is in lsr.json in this folder.</p>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
 const htf = [{d['lat']}, {d['lon']}], pts = {pts};
 const map = L.map('map');
 L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom: 18, attribution: '&copy; OpenStreetMap'}}).addTo(map);
 L.circle(htf, {{radius: {radius} * 1000, color: '#0b5cad', weight: 1, fillOpacity: 0.04}}).addTo(map);
 L.circleMarker(htf, {{radius: 8, color: '#fff', weight: 2, fillColor: '#d9480f', fillOpacity: 1}}).addTo(map).bindTooltip('HTF point {d['htf_id']}', {{permanent: true, direction: 'right'}});
 const b = [htf];
 pts.forEach(p => {{ b.push([p.lat, p.lon]);
   L.marker([p.lat, p.lon], {{icon: L.divIcon({{className: '', html: '<div class="num">' + p.n + '</div>', iconSize: [28, 28], iconAnchor: [14, 14]}})}})
    .addTo(map).bindPopup(p.n + '. ' + p.t + ' · ' + p.c); }});
 const fit = () => {{ map.invalidateSize(); map.fitBounds(L.latLngBounds(b).pad(0.15), {{maxZoom: 13}}); }};
 map.setView(htf, 11); fit();
 window.addEventListener('load', fit); window.addEventListener('resize', fit);
</script>
</body></html>
"""


def write_news_workbook(news_dest, data, names):
    out = news_dest / "HTF_LSR_reports.xlsx"
    if (news_dest / f"~${out.name}").exists():
        print(f"{out.name} is open in Excel; not updated")
        return
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Reports"
    ws.append(["Event folder", "HTF id", "Window start (local)", "Window end (local)", "Report time (local)",
               "Hours from window", "Distance (km)", "Type", "Magnitude", "Unit", "City", "State", "NWS office",
               "Reported by", "Remark", "NWS product", "Final"])
    ev_ws = wb.create_sheet("Events")
    ev_ws.append(["Event folder", "HTF id", "Kind", "Window start (local)", "Window end (local)", "Reports",
                  "Coastal flood reports", "Nearest report (km)", "Checked at (UTC)", "Final"])
    for eid in sorted(data):
        d, w = data[eid], data[eid].get("window", {})
        rel = names.get(eid)
        name = rel.name if rel else eid
        page = (rel / "NWS_reports.html").as_posix() if rel and d["reports"] else None
        for r in d["reports"]:
            ws.append([name, d["htf_id"], w.get("start_local"), w.get("end_local"), r.get("time_local") or r["time"],
                       r["hours_from_window"], r["distance_km"], r["type"], r.get("magnitude"), r.get("unit"),
                       r.get("city"), r.get("state"), r.get("wfo"), r.get("source"), r.get("remark"),
                       r.get("product_url"), d["final"]])
            if r.get("product_url"):
                ws.cell(ws.max_row, 16).hyperlink = r["product_url"]
            if page:
                ws.cell(ws.max_row, 1).hyperlink = page
        ev_ws.append([name, d["htf_id"], d["kind"], w.get("start_local"), w.get("end_local"), d["n_reports"],
                      d["n_coastal_flood"], min((r["distance_km"] for r in d["reports"]), default=None),
                      d["checked_at"], d["final"]])
        if page:
            ev_ws.cell(ev_ws.max_row, 1).hyperlink = page
    for sheet, widths in ((ws, [24, 8, 26, 26, 26, 10, 10, 15, 10, 8, 20, 6, 8, 18, 80, 30, 7]),
                          (ev_ws, [24, 8, 11, 26, 26, 9, 12, 12, 21, 7])):
        for i, wdt in enumerate(widths, 1):
            sheet.column_dimensions[sheet.cell(1, i).column_letter].width = wdt
        for c in sheet[1]:
            c.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
    tmp = news_dest / f".{out.name}.tmp"
    wb.save(tmp)
    tmp.replace(out)
    n_rep = sum(len(d["reports"]) for d in data.values())
    print(f"{out.name}: {n_rep} reports for {sum(bool(d['reports']) for d in data.values())} of {len(data)} events")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="Box Drive folder to sync into")
    ap.add_argument("--s3-bucket", default=S3_BUCKET, help="bucket written by the ECS service ('' to skip)")
    ap.add_argument("--no-artifacts", action="store_true", help="skip GitHub Actions artifacts")
    ap.add_argument("--mirror", default=str(MIRROR), help="local copy of the bucket layout, outside Box")
    ap.add_argument("--news-dest", help="Box folder for NWS storm reports (S3 news/); skipped if not given")
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
