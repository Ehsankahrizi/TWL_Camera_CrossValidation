#!/usr/bin/env python3
"""Copy captured frames into a Box Drive folder, from S3 (ECS) and GitHub artifacts.

ECS deployment: `aws s3 sync s3://<bucket>/events/ DEST` (same layout as below).

The capture workflow uploads each run's frames as an artifact ("captures-<run id>",
kept 90 days). This script downloads every artifact not synced yet into DEST, and
puts the matching event.json next to the images so each event folder is complete:

    DEST/<date>/<event_id>/event.json
    DEST/<date>/<event_id>/<source>_<camera>/<UTC stamp>.jpg

Box Drive then uploads the files. Needs the GitHub CLI (`gh`) logged in.

Usage:
    python3 tools/sync_to_box.py --dest "/Users/<you>/Library/CloudStorage/Box-Box/<folder>"
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "Ehsankahrizi/TWL_Camera_CrossValidation"
S3_BUCKET = "bil6-twl-camera-crossval-858933856877"     # ECS deployment
STATE_FILE = ".synced_artifacts.json"


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="Box Drive folder to sync into")
    ap.add_argument("--s3-bucket", default=S3_BUCKET, help="bucket written by the ECS service ('' to skip)")
    ap.add_argument("--no-artifacts", action="store_true", help="skip GitHub Actions artifacts")
    args = ap.parse_args()

    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    state_path = dest / STATE_FILE
    synced = set(json.loads(state_path.read_text())) if state_path.exists() else set()

    if args.s3_bucket:
        aws = shutil.which("aws")
        if not aws:
            raise RuntimeError("aws CLI not found on PATH")
        r = subprocess.run([aws, "s3", "sync", f"s3://{args.s3_bucket}/events/", str(dest),
                            "--only-show-errors", "--no-progress"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"aws s3 sync failed: {r.stderr.strip()[:300]}")
        print(f"S3 s3://{args.s3_bucket}/events/ synced into {dest}")
    if args.no_artifacts:
        return

    todo = [a for a in list_artifacts() if a[1] not in synced]
    print(f"{len(todo)} new artifact(s) to sync into {dest}")
    events = set()
    for aid, name, run_id in todo:
        with tempfile.TemporaryDirectory() as tmp:
            gh("run", "download", run_id, "-R", REPO, "-n", name, "-D", tmp)
            n = 0
            for f in Path(tmp).rglob("*.jpg"):
                rel = f.relative_to(tmp)                      # <date>/<event_id>/<camera>/<stamp>.jpg
                target = dest / rel
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
            (dest / date / event_id / "event.json").write_text(text)
    print(f"Updated {len(events)} event folder(s)")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(str(e))
