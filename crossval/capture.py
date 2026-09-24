"""Capture camera frames for planned events.

Live capture (runs every 15 min): for events whose capture window contains "now",
grab one frame from each live-only camera (traffic, Windy). For traffic cameras
with an HLS stream, a frame is pulled from the video with ffmpeg (truly current);
otherwise, or if that fails, the agency snapshot image is saved.

Archive backfill: once a window has been closed for BACKFILL_DELAY_MIN, the frames
recorded during the window are downloaded from the USGS HIVIS (and WebCOOS) archives.

Images go to CAPTURE_DIR/<date>/<event_id>/<source>_<camera>/<UTC stamp>.jpg; every
saved frame is also listed in the event's event.json.

Usage:  python -m crossval.capture
"""

import os
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

from . import config
from .sources import WEBCOOS_API, windy_current_image
from .util import http_get, http_json, iso, parse_time, read_json, save_image, stamp, utcnow, write_json

GIVE_UP_AFTER = timedelta(days=3)       # stop trying to backfill an event after this
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
ARTIFACT = f"captures-{RUN_ID}"


def safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_")[:80]


def frame_path(ev, cam, t):
    return (Path(ev["window"]["start"][:10]) / ev["event_id"] / f"{cam['source']}_{safe(cam['id'])}" / f"{stamp(t)}.jpg")


def record(ev, cam, t, rel, method, size):
    ev["captures"].append({"source": cam["source"], "camera_id": cam["id"], "time": iso(t), "method": method,
                           "file": str(rel), "artifact": ARTIFACT, "size": list(size)})


def fetch_bytes(url, timeout=25):
    r = http_get(url, timeout=timeout, retries=2)
    return r.content if r is not None and r.ok else None


def hls_frame(url):
    """One JPEG frame from an HLS stream via ffmpeg, or None."""
    if not shutil.which("ffmpeg"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "f.jpg"
        try:
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-user_agent", config.USER_AGENT,
                            "-rw_timeout", "15000000", "-i", url, "-frames:v", "1", "-q:v", "2", "-y", str(out)],
                           timeout=45, check=True, capture_output=True)
            return out.read_bytes() if out.exists() else None
        except (subprocess.SubprocessError, OSError):
            return None


# ── Live ──────────────────────────────────────────────────────────────────────

def capture_live(ev, now):
    got = 0
    min_gap = timedelta(minutes=config.LIVE_CAPTURE_INTERVAL_MIN - 5)
    for cam in ev["cameras"]:
        if cam["archive"]:
            continue
        last = [parse_time(c["time"]) for c in ev["captures"] if c["camera_id"] == cam["id"]]
        if last and now - max(last) < min_gap:
            continue
        data, method = None, None
        if cam["source"] == "traffic":
            if cam.get("video_url"):
                data, method = hls_frame(cam["video_url"]), "hls_frame"
            if not data and cam.get("image_url"):
                sep = "&" if "?" in cam["image_url"] else "?"
                data, method = fetch_bytes(f"{cam['image_url']}{sep}_t={int(now.timestamp())}"), "snapshot"
        elif cam["source"] == "windy" and config.SAVE_WINDY_IMAGES and config.WINDY_API_KEY:
            url = windy_current_image(cam["id"])
            data, method = (fetch_bytes(url), "windy_current") if url else (None, None)
        if not data:
            continue
        rel = frame_path(ev, cam, now)
        size = save_image(data, config.CAPTURE_DIR / rel)
        if size:
            record(ev, cam, now, rel, method, size)
            got += 1
    return got


# ── Archive backfill ────────────────────────────────────────────────────────

def thin(frames, step_min):
    """Keep at most one (time, ...) frame per step_min minutes."""
    out, last = [], None
    for f in sorted(frames, key=lambda x: x[0]):
        if last is None or f[0] - last >= timedelta(minutes=step_min):
            out.append(f)
            last = f[0]
    return out


def usgs_frames(cam, start, end):
    names = http_json("https://api.waterdata.usgs.gov/nims/v0/listFiles",
                      params={"camId": cam["id"], "after": iso(start), "before": iso(end), "recent": "false", "limit": 500})
    frames = []
    for name in names or []:
        m = re.search(r"___(\d{4}-\d\d-\d\d)T(\d\d)-(\d\d)-(\d\d)Z", str(name))
        if m and cam.get("image_dir"):
            t = parse_time(f"{m[1]}T{m[2]}:{m[3]}:{m[4]}Z")
            frames.append((t, cam["image_dir"] + name))
    return frames


def webcoos_frames(cam, start, end):
    if not config.WEBCOOS_TOKEN:
        return []
    j = http_json(f"{WEBCOOS_API}/elements/",
                  params={"service": cam["stills_service"], "starting_after": iso(start), "starting_before": iso(end)},
                  headers={"Authorization": f"Token {config.WEBCOOS_TOKEN}", "Accept": "application/json"})
    frames = []
    for el in (j or {}).get("results", []):
        d = el.get("data") or {}
        url = (d.get("properties") or {}).get("url")
        t = ((d.get("extents") or {}).get("temporal") or {}).get("min")
        if url and t:
            frames.append((parse_time(t), url))
    return frames


def backfill(ev):
    start, end = parse_time(ev["window"]["capture_start"]), parse_time(ev["window"]["capture_end"])
    got = 0
    for cam in ev["cameras"]:
        if not cam["archive"]:
            continue
        frames = usgs_frames(cam, start, end) if cam["source"] == "usgs" else webcoos_frames(cam, start, end)
        have = {c["time"] for c in ev["captures"] if c["camera_id"] == cam["id"]}
        for t, url in thin(frames, config.ARCHIVE_FRAME_STEP_MIN):
            if iso(t) in have:
                continue
            data = fetch_bytes(url)
            rel = frame_path(ev, cam, t)
            size = save_image(data, config.CAPTURE_DIR / rel) if data else None
            if size:
                record(ev, cam, t, rel, f"{cam['source']}_archive", size)
                got += 1
    return got


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    now = utcnow()
    schedule = read_json(config.SCHEDULE_PATH, {"events": []})
    live_total = arch_total = active = 0
    for item in schedule["events"]:
        if item.get("done"):
            continue
        path = config.REPO_ROOT / item["path"]
        ev = read_json(path)
        if not ev:
            continue
        s, e = parse_time(ev["window"]["capture_start"]), parse_time(ev["window"]["capture_end"])
        changed = False

        if not ev["state"]["live_done"]:
            if s - timedelta(minutes=5) <= now <= e + timedelta(minutes=5):
                active += 1
                n = capture_live(ev, now)
                live_total += n
                changed |= n > 0
                print(f"  live  {ev['event_id']}: +{n} frames")
            elif now > e + timedelta(minutes=5):
                ev["state"]["live_done"] = True
                changed = True

        if not ev["state"]["backfill_done"] and now >= e + timedelta(minutes=config.BACKFILL_DELAY_MIN):
            n = backfill(ev)
            arch_total += n
            print(f"  archive {ev['event_id']}: +{n} frames")
            # Archives can lag; accept an empty result only after GIVE_UP_AFTER.
            if n > 0 or now - e > GIVE_UP_AFTER:
                ev["state"]["backfill_done"] = True
            changed = True

        if changed:
            write_json(path, ev)
            item["done"] = ev["state"]["live_done"] and ev["state"]["backfill_done"]

    write_json(config.SCHEDULE_PATH, schedule)
    print(f"{active} events in their capture window; saved {live_total} live and {arch_total} archived frames")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"frames={live_total + arch_total}\n")


if __name__ == "__main__":
    main()
