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

Box Drive then uploads the files. Needs the GitHub CLI (`gh`) logged in.

Usage:
    python3 tools/sync_to_box.py --dest "/Users/<you>/Library/CloudStorage/Box-Box/<folder>"
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


def event_id_of(ev_dir):
    try:
        return json.loads((ev_dir / "event.json").read_text())["event_id"]
    except (OSError, ValueError, KeyError):
        return None


def name_event_folders(dest, mirror_ids):
    """Rename Box event folders to their HTF/day names; returns {event_id: Box folder Path}.

    Box folders are found by the event_id inside event.json, whatever they are named now
    (original, an earlier scheme, or a name that changed because a second event appeared).
    """
    here = {}                                           # event_id → current Box folder
    for ev_json in dest.glob("*/*/event.json"):
        eid = event_id_of(ev_json.parent)
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="Box Drive folder to sync into")
    ap.add_argument("--s3-bucket", default=S3_BUCKET, help="bucket written by the ECS service ('' to skip)")
    ap.add_argument("--no-artifacts", action="store_true", help="skip GitHub Actions artifacts")
    ap.add_argument("--mirror", default=str(MIRROR), help="local copy of the bucket layout, outside Box")
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
