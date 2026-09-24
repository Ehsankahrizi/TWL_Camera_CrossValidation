#!/usr/bin/env python3
"""Score the HTF forecast against labeled camera evidence.

Each event.json has a "label" block that a person fills in after looking at the
frames:
    "label": {"flooding_observed": true | false | null, "confidence": "high"|"medium"|"low",
              "labeled_by": "EK", "notes": "water over road at 00:15Z"}

Forecast = "exceedance" events (predicted flooding) vs "control" events (predicted
no flooding). Observation = flooding_observed. Unlabeled events are skipped.

    forecast \\ observed    flooding      no flooding
    exceedance              hit           false alarm
    control                 miss          correct negative

Usage:  python3 tools/evaluate.py [--min-confidence medium] [--csv results.csv]
"""

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RANK = {"low": 0, "medium": 1, "high": 2}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-confidence", choices=RANK, default="low")
    ap.add_argument("--csv", help="also write one row per labeled event")
    args = ap.parse_args()

    rows, counts = [], {"hit": 0, "false_alarm": 0, "miss": 0, "correct_negative": 0}
    for path in sorted((ROOT / "events").glob("*/*/event.json")):
        ev = json.loads(path.read_text())
        lab = ev.get("label") or {}
        obs = lab.get("flooding_observed")
        if obs is None or RANK.get(lab.get("confidence") or "low", 0) < RANK[args.min_confidence]:
            continue
        fc = ev["kind"] == "exceedance"
        outcome = ("hit" if obs else "false_alarm") if fc else ("miss" if obs else "correct_negative")
        counts[outcome] += 1
        last = ev["forecasts"][-1] if ev["forecasts"] else {}
        rows.append({"event_id": ev["event_id"], "htf_id": ev["htf_id"], "kind": ev["kind"], "observed": obs,
                     "outcome": outcome, "peak_ft_mhhw": last.get("peak_ft_mhhw"),
                     "threshold_ft_mhhw": ev["threshold_ft_mhhw"], "margin_ft": last.get("margin_ft"),
                     "frames": len(ev["captures"]), "confidence": lab.get("confidence")})

    h, f, m, c = counts["hit"], counts["false_alarm"], counts["miss"], counts["correct_negative"]
    n = h + f + m + c
    print(f"Labeled events: {n}")
    print(f"  hits {h} | false alarms {f} | misses {m} | correct negatives {c}")
    if n:
        pod = h / (h + m) if h + m else float("nan")
        far = f / (h + f) if h + f else float("nan")
        csi = h / (h + f + m) if h + f + m else float("nan")
        print(f"  POD (hit rate)          {pod:.2f}")
        print(f"  FAR (false alarm ratio) {far:.2f}")
        print(f"  CSI (threat score)      {csi:.2f}")
        print(f"  Accuracy                {(h + c) / n:.2f}")
        print("  Note: controls are sampled near-threshold points, not a random sample, so"
              " misses are over-represented relative to the whole coast.")
    if args.csv and rows:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows → {args.csv}")


if __name__ == "__main__":
    main()
