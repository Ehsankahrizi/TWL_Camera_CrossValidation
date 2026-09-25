#!/usr/bin/env python3
"""Score the HTF forecast against the review workbook filled in by a reviewer.

Reads HTF_camera_review.xlsx (made by tools/make_review_sheet.py): one row per
event × camera with "Has the image flooded?" = Yes / No / NaN / Invalid. NaN (photos
cannot be judged) and Invalid (camera cannot show ground flooding, e.g. on a bridge)
are excluded. Per event:
    flooded      if any camera is Yes
    not flooded  if its Yes/No cameras are all No
    skipped      if no camera is Yes or No, or all of the event's frames are stale

    forecast \\ observed    flooding      no flooding
    exceedance              hit           false alarm
    control                 miss          correct negative   (controls are off by default)

Usage:
    python3 tools/evaluate.py --events "<Box folder>" [--review <workbook>] [--csv results.csv]
"""

import argparse
import csv
import json
from pathlib import Path


def read_review(path):
    """{event_id: ["Y"/"N", ...]} from the 'Has the image flooded?' column (Yes/No only)."""
    from openpyxl import load_workbook
    ws = load_workbook(path, read_only=True, data_only=True)["Review"]
    rows = ws.iter_rows(values_only=True)
    head = {h: i for i, h in enumerate(next(rows))}
    answers = {}
    for r in rows:
        eid, ans = r[head["Event ID"]], str(r[head["Has the image flooded?"]] or "").strip().upper()
        ans = {"YES": "Y", "NO": "N"}.get(ans, ans)          # Y/N from older sheets also accepted
        if eid and ans in ("Y", "N"):
            answers.setdefault(eid, []).append(ans)
    return answers


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", required=True, help="folder with <date>/<event_id>/event.json (the Box folder)")
    ap.add_argument("--review", help="review workbook (default: <events>/HTF_camera_review.xlsx)")
    ap.add_argument("--csv", help="also write one row per scored event")
    args = ap.parse_args()
    review = Path(args.review or Path(args.events) / "HTF_camera_review.xlsx")
    answers = read_review(review)

    rows, counts = [], {"hit": 0, "false_alarm": 0, "miss": 0, "correct_negative": 0}
    skipped_stale = 0
    for path in sorted(Path(args.events).glob("*/*/event.json")):
        ev = json.loads(path.read_text())
        a = answers.get(ev["event_id"])
        if not a:
            continue
        fresh = [c for c in ev["captures"] if not c.get("stale")]
        if ev["captures"] and not fresh:
            skipped_stale += 1
            continue
        observed = "Y" in a
        if ev["kind"] == "exceedance":
            outcome = "hit" if observed else "false_alarm"
        else:
            outcome = "miss" if observed else "correct_negative"
        counts[outcome] += 1
        last = ev["forecasts"][-1] if ev["forecasts"] else {}
        rows.append({"event_id": ev["event_id"], "htf_id": ev["htf_id"], "kind": ev["kind"],
                     "window_start_utc": ev["window"]["start"], "window_end_utc": ev["window"]["end"],
                     "cameras_Y": a.count("Y"), "cameras_N": a.count("N"), "flooding_observed": observed,
                     "outcome": outcome, "peak_ft_mhhw": last.get("peak_ft_mhhw"),
                     "threshold_ft_mhhw": ev["threshold_ft_mhhw"], "margin_ft": last.get("margin_ft")})

    h, f, m, c = counts["hit"], counts["false_alarm"], counts["miss"], counts["correct_negative"]
    print(f"Reviewed events: {h + f + m + c}" + (f" (+{skipped_stale} skipped: all frames stale)" if skipped_stale else ""))
    print(f"  hits {h} | false alarms {f}" + (f" | misses {m} | correct negatives {c}" if m + c else ""))
    if h + f:
        print(f"  Success ratio (hits / forecast exceedances)  {h / (h + f):.2f}")
        print(f"  FAR (false alarm ratio)                      {f / (h + f):.2f}")
    if m + c:
        print(f"  POD (hit rate)                               {h / (h + m) if h + m else float('nan'):.2f}")
        print(f"  CSI (threat score)                           {h / (h + f + m) if h + f + m else float('nan'):.2f}")
    else:
        print("  POD / misses need control events (off by default), so they are not computed.")
    if args.csv and rows:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows → {args.csv}")


if __name__ == "__main__":
    main()
