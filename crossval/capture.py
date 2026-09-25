"""Capture camera frames for planned events.

Live capture (runs every 15 min): for events whose capture window contains "now",
grab one frame from each live-only camera (traffic, Windy). For traffic cameras
with an HLS stream, a frame is pulled from the video with ffmpeg (truly current);
otherwise, or if that fails, the agency snapshot image is saved.

Archive backfill: once a window has been closed for BACKFILL_DELAY_MIN, the frames
recorded during the window are downloaded from the USGS HIVIS (and WebCOOS) archives.

Images go to CAPTURE_DIR/<date>/<event_id>/<source>_<camera>/<UTC stamp>.jpg; every
saved frame is also listed in the event's event.json.

Timing: each frame records both when it was fetched ("time") and when the camera
took the image ("image_time", with "image_time_basis" saying how that is known),
plus its age at capture and a "stale" flag (older than STALE_AFTER_MIN). A frame
identical to the camera's previous one (a frozen camera) is not saved again.

Usage:  python -m crossval.capture
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

from . import config
from .sources import WEBCOOS_API, windy_current_image
from .util import (add_local_window, http_date, http_get, http_json, image_pixels, iso, local_iso, parse_time, read_json,
                   save_image, stamp, utcnow, write_json)

GIVE_UP_AFTER = timedelta(days=3)       # stop trying to backfill an event after this
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
# Where the image file lives: a GitHub run artifact, or next to event.json in S3.
ARTIFACT = f"s3://{config.S3_BUCKET}/events" if config.S3_BUCKET else f"captures-{RUN_ID}"


def safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_")[:80]


def frame_path(ev, cam, t):
    return (Path(ev["window"]["start"][:10]) / ev["event_id"] / f"{cam['source']}_{safe(cam['id'])}" / f"{stamp(t)}.jpg")


def record(ev, cam, t, rel, method, size, image_time, basis, digest):
    """Add a saved frame to event.json.

    t          when the frame was fetched (live) or recorded (archive), UTC
    image_time when the camera took the image, if the source says so; None if unknown
    basis      how image_time is known: live_stream | http_last_modified | windy_last_updated
               | usgs_filename | webcoos_timestamp | unknown
    """
    tz = ev.get("time_zone")
    age = round((t - image_time).total_seconds() / 60, 1) if image_time else None
    ev["captures"].append({
        "source": cam["source"], "camera_id": cam["id"], "method": method,
        "time": iso(t), "time_local": local_iso(t, tz),
        "image_time": iso(image_time) if image_time else None,
        "image_time_local": local_iso(image_time, tz),
        "image_time_basis": basis,
        "age_min": age,
        "stale": age is not None and age > config.STALE_AFTER_MIN,
        "sha1": digest,
        "file": str(rel), "artifact": ARTIFACT, "size": list(size)})


def fetch(url, timeout=25):
    """(bytes, Last-Modified as UTC datetime or None), or (None, None)."""
    r = http_get(url, timeout=timeout, retries=2)
    if r is None or not r.ok:
        return None, None
    return r.content, http_date(r.headers.get("Last-Modified"))


def fetch_bytes(url, timeout=25):
    return fetch(url, timeout)[0]


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
        mine = [c for c in ev["captures"] if c["camera_id"] == cam["id"]]
        if mine and now - max(parse_time(c["time"]) for c in mine) < min_gap:
            continue
        data = method = image_time = None
        basis = "unknown"
        if cam["source"] == "traffic":
            # Try both the live video and the agency snapshot; some agencies serve a
            # 1080p snapshot but only a 240p stream (or the reverse). Keep the larger.
            options = []
            if cam.get("video_url"):
                options.append((hls_frame(cam["video_url"]), "hls_frame", now, "live_stream"))
            if cam.get("image_url"):
                sep = "&" if "?" in cam["image_url"] else "?"
                d, lm = fetch(f"{cam['image_url']}{sep}_t={int(now.timestamp())}")
                options.append((d, "snapshot", lm, "http_last_modified" if lm else "unknown"))
            usable = [(image_pixels(o[0]), o) for o in options if o[0]]
            usable = [u for u in usable if u[0] > 0]
            if usable:
                data, method, image_time, basis = max(usable, key=lambda u: u[0])[1]
        elif cam["source"] == "windy" and config.SAVE_WINDY_IMAGES and config.WINDY_API_KEY:
            url, updated = windy_current_image(cam["id"])
            if url:
                data, method = fetch_bytes(url), "windy_current"
                image_time = parse_time(updated) if updated else None
                basis = "windy_last_updated" if image_time else "unknown"
        if not data:
            continue
        if image_time and image_time > now:              # server clocks a few seconds fast
            image_time = now
        digest = hashlib.sha1(data).hexdigest()
        if mine and mine[-1].get("sha1") == digest:
            print(f"    = {cam['source']} {cam['id']}: same image as last time (frozen camera), not saved")
            continue
        rel = frame_path(ev, cam, now)
        size = save_image(data, config.CAPTURE_DIR / rel)
        if size:
            record(ev, cam, now, rel, method, size, image_time, basis, digest)
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
                basis = "usgs_filename" if cam["source"] == "usgs" else "webcoos_timestamp"
                record(ev, cam, t, rel, f"{cam['source']}_archive", size, t, basis, hashlib.sha1(data).hexdigest())
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
        path = config.STATE_ROOT / item["path"]
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
            add_local_window(ev)
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
