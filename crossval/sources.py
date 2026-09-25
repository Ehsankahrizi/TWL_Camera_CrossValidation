"""Find cameras near a point, per source.

Every function returns a list of camera dicts:
    {source, id, name, lat, lon, distance_km, archive, image_url, video_url, page_url, ...}
`archive` is True when past frames can be fetched later (USGS, WebCOOS); traffic and
Windy cameras have no archive and must be captured live during the window.
"""

import math
import re

from . import config
from .util import dist_km, http_json, parse_time, utcnow

# Video hosts that reject playback outside their own sites (checked Sep 2026).
BLOCKED_VIDEO = [r"ncdot\.gov:8887", r"skyvdn\.com/.*[?&]token=", r"trafficland\.com", r"divas\.cloud"]


def _bbox(lat, lon, km):
    dlat = km / 111.0
    dlon = km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


# ── Traffic cameras (state DOT / 511 via Road511) ────────────────────────────

def _pick(*xs):
    return next((x for x in xs if isinstance(x, str) and x), None)


def _parse_road511(c, names):
    p = c.get("properties") or {}
    video = _pick(p.get("video_url"), p.get("hls_url"), p.get("stream_url"))
    if video and (not re.search(r"\.m3u8", video, re.I) or p.get("video_auth_required")
                  or p.get("stream_auth") or any(re.search(b, video, re.I) for b in BLOCKED_VIDEO)):
        video = None
    image = _pick(p.get("image_url"),
                  p["image_urls"][0] if isinstance(p.get("image_urls"), list) and p["image_urls"] else None)
    if not image and isinstance(p.get("url"), str) and re.search(r"/map/Cctv/", p["url"], re.I):
        image = p["url"]
    status = str(p.get("camera_status") or p.get("status") or "").lower()
    online = c.get("is_active") is not False and not re.search(r"offline|out_of_service|disabled|inactive", status)
    return {
        "source": "traffic",
        "id": c["id"],
        "name": c.get("name") or p.get("description") or "Traffic camera",
        "lat": c["latitude"], "lon": c["longitude"],
        "provider": names.get(c.get("source")) or f"511 · {c.get('source')}",
        "road": c.get("road_name") or p.get("roadway") or "",
        "direction": p.get("direction") or "",
        "online": online,
        "archive": False,
        "image_url": image,
        "video_url": video,
        "page_url": _pick(p.get("url"), image) or "https://map.road511.com/",
    }


def traffic_near(lat, lon, km=config.CAMERA_RADIUS_KM):
    s, w, n, e = _bbox(lat, lon, km)
    names, cams, cursor = {}, [], None
    for _ in range(5):                                   # ≤ 500 cameras in a 10 km box
        params = {"type": "cameras", "bbox": f"{w:.5f},{s:.5f},{e:.5f},{n:.5f}", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        j = http_json("https://map.road511.com/api/v1/map/features", params=params)
        if not j:
            break
        for a in j.get("attribution") or []:
            names[a.get("source_code")] = a.get("source_name")
        for c in j.get("data") or []:
            if isinstance(c.get("latitude"), (int, float)) and isinstance(c.get("longitude"), (int, float)):
                cams.append(_parse_road511(c, names))
        cursor = j.get("next_cursor") if j.get("has_more") else None
        if not cursor:
            break
    out = []
    for c in cams:
        c["distance_km"] = round(dist_km(lat, lon, c["lat"], c["lon"]), 3)
        if c["distance_km"] <= km and c["online"] and (c["image_url"] or c["video_url"]):
            out.append(c)
    out.sort(key=lambda c: c["distance_km"])
    return out[:config.MAX_TRAFFIC_CAMERAS]


# ── USGS HIVIS (archive available) ───────────────────────────────────────────

_usgs = None


def _usgs_cameras():
    global _usgs
    if _usgs is None:
        j = http_json("https://api.waterdata.usgs.gov/nims/v0/cameras",
                      params={"returnFields": "camId,camName,lat,lng,newestImageDT,hideCam,nwisId,smallDir,overlayDir"})
        _usgs = [c for c in (j or []) if not c.get("hideCam") and c.get("lat") and c.get("lng")]
    return _usgs


def usgs_near(lat, lon, km=config.CAMERA_RADIUS_KM):
    now = utcnow()
    out = []
    for c in _usgs_cameras():
        d = dist_km(lat, lon, float(c["lat"]), float(c["lng"]))
        if d > km or not c.get("newestImageDT"):
            continue
        if (now - parse_time(c["newestImageDT"])).days > 7:     # inactive camera
            continue
        out.append({
            "source": "usgs", "id": c["camId"], "name": c.get("camName") or c["camId"],
            "lat": float(c["lat"]), "lon": float(c["lng"]), "distance_km": round(d, 3),
            "provider": "USGS HIVIS", "archive": True,
            # Full-resolution frames (overlayDir); smallDir holds 720 px copies.
            "image_dir": c.get("overlayDir") or c.get("smallDir"), "nwis_id": c.get("nwisId"),
            "page_url": f"https://apps.usgs.gov/hivis/camera/{c['camId']}",
        })
    return sorted(out, key=lambda c: c["distance_km"])


# ── WebCOOS (archive; needs the WEBCOOS_TOKEN secret) ────────────────────────

WEBCOOS_API = "https://app.webcoos.org/webcoos/api/v1"
_webcoos = None


def _webcoos_assets():
    global _webcoos
    if _webcoos is None:
        _webcoos = []
        if not config.WEBCOOS_TOKEN:
            return _webcoos
        j = http_json(f"{WEBCOOS_API}/assets/",
                      headers={"Authorization": f"Token {config.WEBCOOS_TOKEN}", "Accept": "application/json"})
        for a in (j or {}).get("results", []):
            d = a.get("data") or {}
            coords = ((d.get("properties") or {}).get("location") or {}).get("coordinates")
            services = [s for f in a.get("feeds") or [] for p in f.get("products") or [] for s in p.get("services") or []]
            stills = next((s["data"]["common"]["slug"] for s in services
                           if "-stills" in (((s.get("data") or {}).get("common") or {}).get("slug") or "")), None)
            if coords and stills:
                _webcoos.append({"slug": (d.get("common") or {}).get("slug"),
                                 "name": (d.get("common") or {}).get("label"),
                                 "lat": float(coords[1]), "lon": float(coords[0]), "stills_service": stills})
    return _webcoos


def webcoos_near(lat, lon, km=config.CAMERA_RADIUS_KM):
    out = []
    for a in _webcoos_assets():
        d = dist_km(lat, lon, a["lat"], a["lon"])
        if d <= km:
            out.append({"source": "webcoos", "id": a["slug"], "name": a["name"], "lat": a["lat"], "lon": a["lon"],
                        "distance_km": round(d, 3), "provider": "WebCOOS", "archive": True,
                        "stills_service": a["stills_service"],
                        "page_url": f"https://webcoos.org/cameras/{a['slug']}/"})
    return sorted(out, key=lambda c: c["distance_km"])


# ── Windy webcams (live only; needs API_WWW_WINDY_COM) ───────────────────────

WINDY_API = "https://api.windy.com/webcams/api/v3/webcams"


def windy_near(lat, lon, km=config.CAMERA_RADIUS_KM):
    if not config.WINDY_API_KEY:
        return []
    j = http_json(WINDY_API, params={"nearby": f"{lat:.4f},{lon:.4f},{max(1, round(km))}", "limit": 50,
                                     "include": "location,urls,player", "lang": "en"},
                  headers={"x-windy-api-key": config.WINDY_API_KEY, "Accept": "application/json"})
    out = []
    for w in (j or {}).get("webcams", []):
        loc = w.get("location") or {}
        if loc.get("latitude") is None or w.get("status", "active") != "active":
            continue
        d = dist_km(lat, lon, float(loc["latitude"]), float(loc["longitude"]))
        if d <= km:
            wid = w.get("webcamId") or w.get("id")
            out.append({"source": "windy", "id": str(wid), "name": w.get("title") or "Windy webcam",
                        "lat": float(loc["latitude"]), "lon": float(loc["longitude"]), "distance_km": round(d, 3),
                        "provider": "Windy.com", "archive": False,
                        "page_url": (w.get("urls") or {}).get("detail") or f"https://www.windy.com/webcams/{wid}",
                        "player_day": (w.get("player") or {}).get("day")})
    out.sort(key=lambda c: c["distance_km"])
    return out[:config.MAX_WINDY_CAMERAS]


def windy_current_image(webcam_id):
    """(fresh 10-minute image URL, time the image was taken per Windy) or (None, None)."""
    j = http_json(f"{WINDY_API}/{webcam_id}", params={"include": "images"},
                  headers={"x-windy-api-key": config.WINDY_API_KEY, "Accept": "application/json"})
    img = ((j or {}).get("images") or {}).get("current") or {}
    return img.get("preview") or img.get("thumbnail"), (j or {}).get("lastUpdatedOn")


def cameras_near(lat, lon):
    cams = traffic_near(lat, lon) + usgs_near(lat, lon) + webcoos_near(lat, lon) + windy_near(lat, lon)
    for c in cams:
        c.pop("online", None)
    return cams
