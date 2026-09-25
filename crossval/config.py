"""Settings shared by the planner, the live capture and the archive backfill."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Forecast input (published by github.com/Ehsankahrizi/coastal-twl-app) ──
FORECAST_BASE = "https://ehsankahrizi.github.io/coastal-twl-app/data"
FORECAST_FILE = "nwm_htf_5km.json"      # mean NWM TWL (ft MHHW) per HTF point, 5 km NWM radius
METADATA_FILE = "metadata.json"

# ── Cameras ──
CAMERA_RADIUS_KM = 5.0                  # a camera "looks at" an HTF point if it is this close
MAX_TRAFFIC_CAMERAS = 5                 # nearest traffic cameras kept per event
MAX_WINDY_CAMERAS = 3

# ── Capture windows ──
# Forecasts are hourly. An event's window runs from the first to the last hour at or
# above the threshold, padded on both sides so the rise and fall are recorded too.
WINDOW_PAD_MIN = 30
LIVE_CAPTURE_INTERVAL_MIN = 15          # capture workflow cadence (cron */15)
BACKFILL_DELAY_MIN = 60                 # wait this long after a window closes before pulling archives
ARCHIVE_FRAME_STEP_MIN = 15             # keep at most one archived frame per this many minutes

# ── Control cases (forecast BELOW threshold) ──
CONTROL_MAX_PER_RUN = 15                # sampled per forecast run, nearest-to-threshold first
CONTROL_WINDOW_HALF_MIN = 60            # capture ±60 min around the forecast peak

# ── Images ──
IMAGE_MAX_WIDTH = 1920                  # full HD kept as is; larger frames are downsized
JPEG_QUALITY = 85

# ── Storage ──
# GitHub Actions: metadata lives in the repo, images in run artifacts.
# ECS (S3_BUCKET set): metadata and images live in S3; each cycle works in a local
# copy under STATE_DIR that crossval.store downloads before and uploads after.
S3_BUCKET = os.environ.get("S3_BUCKET", "").strip()
STATE_ROOT = Path(os.environ.get("STATE_DIR", REPO_ROOT))
EVENTS_DIR = STATE_ROOT / "events"      # events/<date>/<event_id>/event.json
SCHEDULE_PATH = STATE_ROOT / "schedule" / "schedule.json"
CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", STATE_ROOT / "captures"))  # images

# ── Credentials (GitHub repository secrets; never hard-coded) ──
WINDY_API_KEY = os.environ.get("API_WWW_WINDY_COM", "").strip()
WEBCOOS_TOKEN = os.environ.get("WEBCOOS_TOKEN", "").strip()
# Windy's API terms restrict storing webcam images. Off by default: Windy webcams are
# still listed in event.json with their links. Set repository variable
# SAVE_WINDY_IMAGES=true only if your use is permitted.
SAVE_WINDY_IMAGES = os.environ.get("SAVE_WINDY_IMAGES", "false").lower() == "true"

USER_AGENT = "TWL-Camera-CrossValidation/1.0 (research; github.com/Ehsankahrizi/TWL_Camera_CrossValidation)"
