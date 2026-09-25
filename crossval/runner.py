"""Always-on scheduler for the ECS service (no EventBridge needed).

Runs a capture cycle at :00, :15, :30 and :45 past every hour, and a planning cycle
at 01:50, 07:50, 13:50 and 19:50 UTC (20 min after each coastal-twl-app forecast run).
Each cycle pulls the state from S3, runs, and pushes what changed. A failing cycle is
logged and the loop continues; if the process dies, ECS restarts the container.

Usage (inside the container):  python -m crossval.runner
"""

import time
import traceback
from datetime import timedelta

from . import capture, config, plan, store
from .util import utcnow

PLAN_TIMES = {(1, 50), (7, 50), (13, 50), (19, 50)}
CAPTURE_EVERY_MIN = config.LIVE_CAPTURE_INTERVAL_MIN


def run_cycle(name, fn):
    t0 = time.time()
    print(f"── {name} cycle {utcnow():%Y-%m-%d %H:%M:%S}Z", flush=True)
    try:
        store.pull_state()
        fn()
        n = store.push_state()
        print(f"── {name} done in {time.time() - t0:.0f} s, uploaded {n} files", flush=True)
    except (Exception, SystemExit):          # plan.main exits if the forecast can't be fetched
        traceback.print_exc()
        print(f"── {name} FAILED after {time.time() - t0:.0f} s", flush=True)


def next_slot(now):
    """Next whole quarter hour (or planning time) after now."""
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while not (t.minute % CAPTURE_EVERY_MIN == 0 or (t.hour, t.minute) in PLAN_TIMES):
        t += timedelta(minutes=1)
    return t


def main():
    if not config.S3_BUCKET:
        raise SystemExit("S3_BUCKET is not set")
    store.load_windy_key()
    print(f"Runner started: bucket {config.S3_BUCKET}, capture every {CAPTURE_EVERY_MIN} min, "
          f"plan at {sorted(PLAN_TIMES)} UTC", flush=True)
    run_cycle("capture", capture.main)          # catch up immediately after a (re)start
    while True:
        slot = next_slot(utcnow())
        time.sleep(max(0, (slot - utcnow()).total_seconds()))
        if (slot.hour, slot.minute) in PLAN_TIMES:
            run_cycle("plan", plan.main)
        if slot.minute % CAPTURE_EVERY_MIN == 0:
            run_cycle("capture", capture.main)


if __name__ == "__main__":
    main()
