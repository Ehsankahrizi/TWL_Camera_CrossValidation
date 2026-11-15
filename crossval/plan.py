"""Plan capture events from the latest HTF forecast.

For every HTF point whose mean NWM forecast reaches its threshold, one "exceedance"
event is created per HIGH TIDE that reaches it: the hours at/above the threshold
around one tidal peak. A long surge that keeps the water above the threshold for days
is split at the lowest point between peaks, so each event is one high tide and a
reviewer answers one question per tide. A sample of
points forecast to stay BELOW their threshold becomes "control" events around their
forecast peak, so misses (flooding the forecast did not predict) can be measured too.

Consecutive forecast runs overlap (18-hour horizon, new run every 6 h). An event
already planned for the same HTF point and the same high tide (peaks less than
TIDE_MATCH apart) is updated rather than duplicated, and every run's forecast is kept
in event.json.

Usage:  python -m crossval.plan
"""

from datetime import timedelta

from . import config
from .sources import cameras_near
from .util import add_local_window, event_date, http_json, iso, parse_time, read_json, utcnow, write_json

FT_PER_M = 3.280839895
MERGE_GAP = timedelta(hours=3)      # hours at/above threshold this close belong to the same high tide
PEAK_HALF_WIDTH = timedelta(hours=5)  # a tidal peak is the highest value within ±5 h
TIDE_MATCH = timedelta(hours=6)     # half a semi-diurnal cycle (12.4 h): peaks closer than this are one tide


def load_forecast():
    fc = http_json(f"{config.FORECAST_BASE}/{config.FORECAST_FILE}", params={"v": int(utcnow().timestamp())})
    meta = http_json(f"{config.FORECAST_BASE}/{config.METADATA_FILE}", params={"v": int(utcnow().timestamp())})
    if not fc or not meta:
        raise SystemExit("Could not download the forecast from GitHub Pages")
    return fc, meta


def threshold_ft(entry):
    """Threshold in ft above MHHW, whichever unit the pipeline wrote."""
    v = entry["htfMidThreshold"]
    return v if entry.get("units") == "ft" else v * FT_PER_M


def series(entry):
    return sorted(((parse_time(p["validTime"]), p["value"]) for p in entry["meanForecast"]), key=lambda x: x[0])


def exceed_windows(entry):
    """Runs of hourly values ≥ threshold (gaps < MERGE_GAP merged) → [(start, end, peak_time, peak_ft, n_hours)]."""
    thr, out, cur = threshold_ft(entry), [], None
    for t, v in series(entry):
        if v >= thr:
            if cur and t - cur["end"] < MERGE_GAP:
                cur["end"] = t
                cur["n"] += 1
                if v > cur["peak"]:
                    cur["peak"], cur["peak_t"] = v, t
            else:
                cur = {"start": t, "end": t, "peak": v, "peak_t": t, "n": 1}
                out.append(cur)
    return [(w["start"], w["end"], w["peak_t"], w["peak"], w["n"]) for w in out]


def tide_windows(entry):
    """One window per high tide reaching the threshold → [(start, end, peak_time, peak_ft, n_hours)].

    Peaks are hours at/above the threshold that are the highest value within
    ±PEAK_HALF_WIDTH (plateaus: the first hour). Consecutive peaks are separated at the
    lowest hour between them, whether or not that hour is below the threshold. Each
    tide's window runs from its first to its last hour at/above the threshold."""
    thr, pts = threshold_ft(entry), series(entry)
    peaks = []
    for i, (t, v) in enumerate(pts):
        if v < thr:
            continue
        near = [w for u, w in pts if abs(u - t) <= PEAK_HALF_WIDTH]
        if v < max(near):
            continue
        if peaks and t - pts[peaks[-1]][0] < TIDE_MATCH:   # same tide: keep the higher one
            if v > pts[peaks[-1]][1]:
                peaks[-1] = i
        else:
            peaks.append(i)
    if not peaks:
        return []
    # boundaries: the lowest hour between consecutive peaks
    cuts = [min(range(a + 1, b), key=lambda k: pts[k][1]) if b - a > 1 else b for a, b in zip(peaks, peaks[1:])]
    edges = [0] + cuts + [len(pts)]
    out = []
    for (lo, hi), pk in zip(zip(edges, edges[1:]), peaks):
        above = [k for k in range(lo, hi) if pts[k][1] >= thr]
        if above:
            out.append((pts[above[0]][0], pts[above[-1]][0], pts[pk][0], pts[pk][1], len(above)))
    return out


def peak_seen(entry, peak_t):
    """True if the run covers PEAK_HALF_WIDTH on both sides of the peak. A peak at the
    start or end of a run's 18-h series may only be the edge of a rising/falling tide."""
    pts = series(entry)
    return pts[0][0] <= peak_t - PEAK_HALF_WIDTH and peak_t + PEAK_HALF_WIDTH <= pts[-1][0]


def event_peak(ev):
    """Peak time of an event's high tide (tide_peak; for events planned before
    2026-11-14, the newest forecast's peak)."""
    return parse_time(ev.get("tide_peak") or ev["forecasts"][-1]["peak_time"])


def forecast_record(entry, meta, peak_t, peak, n_hours):
    return {
        "pipeline_run": meta.get("lastUpdated"),
        "nwm_creation_time": entry.get("creationTime"),
        "peak_ft_mhhw": round(peak, 3),
        "peak_time": iso(peak_t),
        "hours_at_or_above": n_hours,
        "margin_ft": round(peak - threshold_ft(entry), 3),       # >0 exceeds, <0 below
        "series_ft_mhhw": [{"t": iso(t), "v": round(v, 3)} for t, v in series(entry)],
    }


def event_path(event):
    return config.EVENTS_DIR / event_date(event) / event["event_id"] / "event.json"


def new_event(kind, entry, start, end, pad_min, cams):
    s, e = start - timedelta(minutes=pad_min), end + timedelta(minutes=pad_min)
    eid = f"{start:%Y%m%dT%H}Z_HTF{int(entry['htfId']):04d}_{kind}"
    rng = entry.get("htfRange", "")
    return {
        "event_id": eid,
        "kind": kind,                                  # "exceedance" | "control"
        "htf_id": int(entry["htfId"]),
        "lat": entry["lat"], "lon": entry["lon"],
        "time_zone": entry.get("timeZone"),
        "threshold_ft_mhhw": round(threshold_ft(entry), 4),
        "threshold_m_mhhw": round(threshold_ft(entry) / FT_PER_M, 6),
        "htf_range_m": rng,
        "nwm_stations": entry.get("matchedStations", []),
        "window": {"start": iso(start), "end": iso(end), "capture_start": iso(s), "capture_end": iso(e)},
        "forecasts": [],
        "cameras": cams,
        "captures": [],
        "state": {"live_done": not any(not c["archive"] for c in cams),
                  "backfill_done": not any(c["archive"] for c in cams)},
    }


def add_new_cameras(ev, entry):
    """Attach cameras that became available since the event was planned (e.g. Windy
    after its key was added), and reopen the capture steps they need."""
    known = {(c["source"], c["id"]) for c in ev["cameras"]}
    new = [c for c in cameras_near(entry["lat"], entry["lon"]) if (c["source"], c["id"]) not in known]
    if not new:
        return
    ev["cameras"].extend(new)
    if parse_time(ev["window"]["capture_end"]) > utcnow() and any(not c["archive"] for c in new):
        ev["state"]["live_done"] = False
    if any(c["archive"] for c in new):
        ev["state"]["backfill_done"] = False
    print(f"  ~ {ev['event_id']}: +{len(new)} new cameras ({', '.join(sorted({c['source'] for c in new}))})")


def reopen(ev, old_end, now):
    """A newer forecast moved the window's end later. If capture had already finished
    (the old end had passed before this forecast arrived), start it again, or the rest
    of the event is never recorded; archive frames are fetched again for the new end."""
    new_end = parse_time(ev["window"]["capture_end"])
    if new_end <= old_end:
        return
    if ev["state"]["live_done"] and new_end > now and any(not c["archive"] for c in ev["cameras"]):
        ev["state"]["live_done"] = False
        print(f"  ~ {ev['event_id']}: window extended to {iso(new_end)}, live capture reopened")
    if ev["state"]["backfill_done"] and any(c["archive"] for c in ev["cameras"]):
        ev["state"]["backfill_done"] = False


def same_event(ev, kind, htf_id, s, e, peak_t=None, seen=True, start=None, end=None):
    """Same HTF point and kind, and
    exceedance: the same high tide. Peaks less than TIDE_MATCH apart; or, when either
      peak is provisional (at the edge of a run's series, see peak_seen), the hours
      at/above the threshold overlap.
    control: overlapping capture windows."""
    if ev["kind"] != kind or ev["htf_id"] != htf_id:
        return False
    if kind == "exceedance" and ev.get("forecasts"):
        if abs(event_peak(ev) - peak_t) < TIDE_MATCH:
            return True
        if not (seen and ev.get("tide_peak_seen", True)):
            w = ev["window"]
            return parse_time(w["start"]) <= end and start <= parse_time(w["end"])
        return False
    return parse_time(ev["window"]["capture_start"]) <= e and s <= parse_time(ev["window"]["capture_end"])


def main():
    fc, meta = load_forecast()
    now = utcnow()
    schedule = read_json(config.SCHEDULE_PATH, {"events": []})
    events = {x["event_id"]: read_json(config.STATE_ROOT / x["path"]) for x in schedule["events"]}
    events = {k: v for k, v in events.items() if v}
    run = meta.get("lastUpdated")
    print(f"Forecast run {run}: {len(fc)} HTF points with NWM data")

    def upsert(kind, entry, start, end, pad, rec):
        s, e = start - timedelta(minutes=pad), end + timedelta(minutes=pad)
        if e < now - timedelta(hours=6):
            return None                                    # too old to capture anything
        peak_t = parse_time(rec["peak_time"])
        seen = peak_seen(entry, peak_t)
        # the best match: the same tide with the nearest peak
        # (an event this run already updated with another tide is not matched again,
        # so one run's two tides never merge into one event)
        matches = [ev for ev in events.values()
                   if same_event(ev, kind, int(entry["htfId"]), s, e, peak_t, seen, start, end)
                   and not any(f["pipeline_run"] == run and f["peak_time"] != rec["peak_time"] for f in ev["forecasts"])]
        if kind == "exceedance":
            matches.sort(key=lambda ev: abs(event_peak(ev) - peak_t))
        for ev in matches[:1]:
            if not any(f["pipeline_run"] == run for f in ev["forecasts"]):
                ev["forecasts"].append(rec)
            if kind == "exceedance" and not ev.get("tide_peak_seen", True):
                # The tide's peak is fixed once a run has seen it whole; until then the
                # newest provisional peak is kept. A fixed anchor stops a slow surge from
                # drifting one event across several tides, run after run.
                ev["tide_peak"], ev["tide_peak_seen"] = rec["peak_time"], seen
            w = ev["window"]
            w["start"] = iso(min(parse_time(w["start"]), start))
            w["end"] = iso(max(parse_time(w["end"]), end))
            w["capture_start"] = iso(min(parse_time(w["capture_start"]), s))
            old_end = parse_time(w["capture_end"])
            w["capture_end"] = iso(max(old_end, e))
            reopen(ev, old_end, now)
            add_new_cameras(ev, entry)
            return ev
        cams = cameras_near(entry["lat"], entry["lon"])
        if e < now:                                        # window over: only archives can still help
            cams = [c for c in cams if c["archive"]]
        if not cams:
            return None
        ev = new_event(kind, entry, start, end, pad, cams)
        ev["forecasts"].append(rec)
        if kind == "exceedance":
            ev["tide_peak"], ev["tide_peak_seen"] = rec["peak_time"], seen
        events[ev["event_id"]] = ev
        print(f"  + {ev['event_id']}: {len(cams)} cameras "
              f"({', '.join(sorted({c['source'] for c in cams}))}), window {ev['window']['start']} → {ev['window']['end']}")
        return ev

    # Exceedances
    n_exc = 0
    for entry in fc.values():
        for start, end, peak_t, peak, n in tide_windows(entry):
            if upsert("exceedance", entry, start, end, config.WINDOW_PAD_MIN,
                      forecast_record(entry, meta, peak_t, peak, n)):
                n_exc += 1

    # Controls: below-threshold points, closest to their threshold first
    below = []
    for entry in fc.values():
        if exceed_windows(entry):
            continue
        t, v = max(series(entry), key=lambda x: x[1])
        below.append((threshold_ft(entry) - v, entry, t, v))
    below.sort(key=lambda x: x[0])
    n_ctl = checked = 0
    for margin, entry, peak_t, peak in below:
        if n_ctl >= config.CONTROL_MAX_PER_RUN or checked >= 4 * config.CONTROL_MAX_PER_RUN:
            break
        checked += 1
        if upsert("control", entry, peak_t, peak_t, config.CONTROL_WINDOW_HALF_MIN,
                  forecast_record(entry, meta, peak_t, peak, 0)):
            n_ctl += 1

    for ev in events.values():
        add_local_window(ev)
        write_json(event_path(ev), ev)
    schedule = {
        "updated": iso(now),
        "pipeline_run": run,
        # Finished events leave the schedule after 2 days; their event.json stays.
        "events": sorted(({"event_id": ev["event_id"], "kind": ev["kind"],
                           "path": str(event_path(ev).relative_to(config.STATE_ROOT)),
                           "capture_start": ev["window"]["capture_start"], "capture_end": ev["window"]["capture_end"],
                           "done": ev["state"]["live_done"] and ev["state"]["backfill_done"]}
                          for ev in events.values()
                          if not (ev["state"]["live_done"] and ev["state"]["backfill_done"])
                          or parse_time(ev["window"]["capture_end"]) > now - timedelta(days=2)),
                         key=lambda x: x["capture_start"]),
    }
    write_json(config.SCHEDULE_PATH, schedule)
    print(f"Planned/updated {n_exc} exceedance and {n_ctl} control events; {len(events)} events tracked")


if __name__ == "__main__":
    main()
