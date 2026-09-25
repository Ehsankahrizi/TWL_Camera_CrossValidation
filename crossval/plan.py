"""Plan capture events from the latest HTF forecast.

For every HTF point whose mean NWM forecast reaches its threshold, one "exceedance"
event is created per contiguous run of hours at/above the threshold. A sample of
points forecast to stay BELOW their threshold becomes "control" events around their
forecast peak, so misses (flooding the forecast did not predict) can be measured too.

Consecutive forecast runs overlap (18-hour horizon, new run every 6 h). An event
already planned for the same HTF point and an overlapping window is updated rather
than duplicated, and every run's forecast is kept in event.json.

Usage:  python -m crossval.plan
"""

from datetime import timedelta

from . import config
from .sources import cameras_near
from .util import add_local_window, http_json, iso, parse_time, read_json, utcnow, write_json

FT_PER_M = 3.280839895
MERGE_GAP = timedelta(hours=3)      # hours at/above threshold this close belong to the same high tide


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
    return config.EVENTS_DIR / event["window"]["start"][:10] / event["event_id"] / "event.json"


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
        "label": {"flooding_observed": None, "confidence": None, "labeled_by": None, "notes": ""},
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


def overlaps(ev, kind, htf_id, s, e):
    if ev["kind"] != kind or ev["htf_id"] != htf_id:
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
        for ev in events.values():
            if overlaps(ev, kind, int(entry["htfId"]), s, e):
                if not any(f["pipeline_run"] == run for f in ev["forecasts"]):
                    ev["forecasts"].append(rec)
                w = ev["window"]
                w["start"] = iso(min(parse_time(w["start"]), start))
                w["end"] = iso(max(parse_time(w["end"]), end))
                w["capture_start"] = iso(min(parse_time(w["capture_start"]), s))
                w["capture_end"] = iso(max(parse_time(w["capture_end"]), e))
                add_new_cameras(ev, entry)
                return ev
        cams = cameras_near(entry["lat"], entry["lon"])
        if e < now:                                        # window over: only archives can still help
            cams = [c for c in cams if c["archive"]]
        if not cams:
            return None
        ev = new_event(kind, entry, start, end, pad, cams)
        ev["forecasts"].append(rec)
        events[ev["event_id"]] = ev
        print(f"  + {ev['event_id']}: {len(cams)} cameras "
              f"({', '.join(sorted({c['source'] for c in cams}))}), window {ev['window']['start']} → {ev['window']['end']}")
        return ev

    # Exceedances
    n_exc = 0
    for entry in fc.values():
        for start, end, peak_t, peak, n in exceed_windows(entry):
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
