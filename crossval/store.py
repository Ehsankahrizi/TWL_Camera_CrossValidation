"""S3 storage for the ECS deployment.

S3 layout (bucket = S3_BUCKET), identical to the Box folder layout:
    schedule/schedule.json
    events/<date>/<event_id>/event.json
    events/<date>/<event_id>/<source>_<camera>/<UTC stamp>.jpg
    config/windy_api_key          (private; uploaded by the owner, read at start-up)

Each cycle downloads schedule.json and the event.json files it lists into STATE_DIR,
runs, then uploads every file written during the cycle (util.WRITTEN).
"""

import boto3
from botocore.exceptions import ClientError

from . import config, util

_s3 = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _key(path):
    return str(path.relative_to(config.STATE_ROOT))


def _download(key):
    dest = config.STATE_ROOT / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        s3().download_file(config.S3_BUCKET, key, str(dest))
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def pull_state():
    """Fresh local copy of schedule.json and every event.json it lists."""
    util.WRITTEN.clear()
    if not _download(_key(config.SCHEDULE_PATH)):
        return
    schedule = util.read_json(config.SCHEDULE_PATH, {"events": []})
    for item in schedule["events"]:
        _download(item["path"])


def push_state():
    """Upload everything written since pull_state()."""
    n = 0
    for path in sorted(util.WRITTEN):
        if not path.exists():
            continue
        extra = {"ContentType": {".jpg": "image/jpeg", ".png": "image/png"}.get(path.suffix, "application/json")}
        s3().upload_file(str(path), config.S3_BUCKET, _key(path), ExtraArgs=extra)
        if path.suffix in (".jpg", ".png"):
            path.unlink()                        # keep the container's disk small
        n += 1
    util.WRITTEN.clear()
    return n


def load_windy_key():
    """Windy key from s3://<bucket>/config/windy_api_key, unless already in the environment."""
    if config.WINDY_API_KEY:
        return
    try:
        obj = s3().get_object(Bucket=config.S3_BUCKET, Key="config/windy_api_key")
        config.WINDY_API_KEY = obj["Body"].read().decode().strip()
        print("Windy key loaded from S3")
    except ClientError:
        print("No Windy key in S3 (config/windy_api_key); Windy webcams are skipped")
