"""Small helpers: HTTP with per-host throttling, distances, UTC times, JSON and images."""

import io
import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

from . import config

# Every file written in this process, so crossval.store can upload them to S3.
WRITTEN = set()

_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT
_last_call = {}

# Minimum seconds between requests to the same host (Road511 allows 60/min).
HOST_INTERVAL = {"map.road511.com": 1.1}


def http_get(url, *, params=None, headers=None, timeout=30, retries=3):
    """GET with per-host throttling and retries on 429/5xx. Returns Response or None."""
    host = urlparse(url).netloc
    for attempt in range(retries):
        wait = HOST_INTERVAL.get(host, 0) - (time.time() - _last_call.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            print(f"    ! {host}: {e.__class__.__name__}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            reset = r.headers.get("x-ratelimit-reset")
            pause = max(2, min(60, int(reset) - time.time() + 1)) if reset and reset.isdigit() else 5 * (attempt + 1)
            print(f"    ! {host}: HTTP {r.status_code}, waiting {pause:.0f} s")
            time.sleep(pause)
            continue
        return r
    return None


def http_json(url, **kw):
    r = http_get(url, **kw)
    if r is None or not r.ok:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def dist_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(s):
    """ISO 8601 → aware UTC datetime."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_iso(dt, tz_name):
    """Same instant in the point's local time, e.g. 2026-09-24T19:12:11-04:00 (None if no zone)."""
    if not dt or not tz_name:
        return None
    try:
        return dt.astimezone(ZoneInfo(tz_name)).isoformat(timespec="seconds")
    except (ZoneInfoNotFoundError, ValueError):
        return None


def add_local_window(ev):
    """window.*_local: the UTC window bounds in the HTF point's own time zone."""
    w = ev["window"]
    for k in ("start", "end", "capture_start", "capture_end"):
        w[f"{k}_local"] = local_iso(parse_time(w[k]), ev.get("time_zone"))


def http_date(value):
    """Parse an HTTP date header (e.g. Last-Modified) → aware UTC datetime, or None."""
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc) if value else None
    except (TypeError, ValueError):
        return None


def stamp(dt):
    """Filename-safe UTC stamp, e.g. 2026-09-25T00-15Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%MZ")


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")
    tmp.replace(path)
    WRITTEN.add(path)


def save_image(data, path):
    """Validate, downsize and save image bytes as JPEG. Returns (width, height) or None."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    if img.width < 80 or img.height < 60:          # placeholders / error icons
        return None
    img = img.convert("RGB")
    if img.width > config.IMAGE_MAX_WIDTH:
        h = round(img.height * config.IMAGE_MAX_WIDTH / img.width)
        img = img.resize((config.IMAGE_MAX_WIDTH, h), Image.LANCZOS)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=config.JPEG_QUALITY, optimize=True)
    WRITTEN.add(path)
    return img.width, img.height


def image_pixels(data):
    """Width × height of image bytes, or 0 if they are not a usable image."""
    try:
        img = Image.open(io.BytesIO(data))
        return img.width * img.height if img.width >= 80 and img.height >= 60 else 0
    except Exception:
        return 0
