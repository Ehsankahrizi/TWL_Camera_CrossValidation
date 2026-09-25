"""Location map (map.png) for each camera folder.

Shows the HTF point, the camera-search radius, the camera, a line between them with
the distance, a legend, a scale bar and a north arrow, on an Esri street basemap.
Rendered with Pillow from web-mercator tiles, so no GIS libraries are needed.

Usage (existing folders):  python -m crossval.maps --events "<folder with <date>/<event_id>/event.json>"
"""

import argparse
import io
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from . import config
from .util import http_get

TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"
ATTRIBUTION = "Basemap: Esri, HERE, Garmin, USGS, NOAA, © OpenStreetMap contributors"
W, H = 1100, 850
RADIUS_PX = 330                      # target on-screen size of the search radius
COLORS = {"htf": (200, 30, 30), "radius": (31, 78, 121), "line": (40, 40, 40),
          "traffic": (37, 99, 235), "usgs": (13, 148, 136), "windy": (147, 51, 234), "webcoos": (234, 88, 12)}
SOURCE_NAME = {"traffic": "Traffic camera (state DOT)", "usgs": "USGS river/coast camera",
               "windy": "Windy webcam", "webcoos": "WebCOOS coastal camera"}

_tiles = {}


def font(size, bold=False):
    return ImageFont.load_default(size=size)


def world_px(lat, lon, z):
    """Web-mercator pixel coordinates of lat/lon at zoom z."""
    n = 256 * 2 ** z
    x = (lon + 180) / 360 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def metres_per_px(lat, z):
    return 156543.03392 * math.cos(math.radians(lat)) / 2 ** z


def tile(z, x, y):
    key = (z, x, y)
    if key not in _tiles:
        r = http_get(TILE_URL.format(z=z, x=x, y=y), timeout=20, retries=2)
        try:
            _tiles[key] = Image.open(io.BytesIO(r.content)).convert("RGB") if r is not None and r.ok else None
        except Exception:
            _tiles[key] = None
    return _tiles[key]


def basemap(lat, lon, z):
    cx, cy = world_px(lat, lon, z)
    x0, y0 = cx - W / 2, cy - H / 2
    img = Image.new("RGB", (W, H), (235, 235, 235))
    for tx in range(int(x0 // 256), int((x0 + W) // 256) + 1):
        for ty in range(int(y0 // 256), int((y0 + H) // 256) + 1):
            t = tile(z, tx % 2 ** z, ty)
            if t:
                img.paste(t, (int(tx * 256 - x0), int(ty * 256 - y0)))
    return img, (x0, y0)


def dashed_circle(draw, c, r, color, width=3, dash_deg=6):
    for a in range(0, 360, dash_deg * 2):
        draw.arc([c[0] - r, c[1] - r, c[0] + r, c[1] + r], a, a + dash_deg, fill=color, width=width)


def marker(draw, p, source, size=13):
    col = COLORS.get(source, (60, 60, 60))
    x, y = p
    if source == "traffic":
        draw.rectangle([x - size, y - size, x + size, y + size], fill=col, outline="white", width=3)
    elif source == "windy":
        draw.polygon([(x, y - size - 3), (x + size + 2, y + size), (x - size - 2, y + size)], fill=col, outline="white", width=3)
    elif source == "webcoos":
        draw.polygon([(x, y - size - 3), (x + size + 3, y), (x, y + size + 3), (x - size - 3, y)], fill=col, outline="white", width=3)
    else:
        draw.ellipse([x - size, y - size, x + size, y + size], fill=col, outline="white", width=3)


def htf_marker(draw, p):
    x, y = p
    draw.ellipse([x - 16, y - 16, x + 16, y + 16], fill="white", outline=COLORS["htf"], width=5)
    draw.ellipse([x - 7, y - 7, x + 7, y + 7], fill=COLORS["htf"])


def local_time(ts, tz):
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    try:
        dt = dt.astimezone(ZoneInfo(tz)) if tz else dt
    except Exception:
        pass
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def render(ev, cam, out_path):
    """Draw map.png for one camera of one event. Returns the path or None on failure."""
    lat, lon, radius_km = ev["lat"], ev["lon"], config.CAMERA_RADIUS_KM
    z = max(10, min(15, math.floor(math.log2(156543.03392 * math.cos(math.radians(lat)) * RADIUS_PX / (radius_km * 1000)))))
    img, (x0, y0) = basemap(lat, lon, z)
    if not any(v is not None for k, v in _tiles.items() if k[0] == z):
        return None                                           # no basemap reachable
    d = ImageDraw.Draw(img, "RGBA")
    to_px = lambda la, lo: (world_px(la, lo, z)[0] - x0, world_px(la, lo, z)[1] - y0)
    hp, cp = to_px(lat, lon), to_px(cam["lat"], cam["lon"])
    r_px = radius_km * 1000 / metres_per_px(lat, z)

    # radius, line, markers
    d.ellipse([hp[0] - r_px, hp[1] - r_px, hp[0] + r_px, hp[1] + r_px], fill=COLORS["radius"] + (22,))
    dashed_circle(d, hp, r_px, COLORS["radius"])
    d.line([hp, cp], fill=COLORS["line"] + (230,), width=4)
    htf_marker(d, hp)
    marker(d, cp, cam["source"])
    dist = f"{cam['distance_km']:.2f} km"
    mid = ((hp[0] + cp[0]) / 2, (hp[1] + cp[1]) / 2)
    tb = d.textbbox(mid, dist, font=font(20), anchor="mm")
    d.rounded_rectangle([tb[0] - 8, tb[1] - 5, tb[2] + 8, tb[3] + 5], 6, fill=(255, 255, 255, 235), outline=COLORS["line"])
    d.text(mid, dist, font=font(20), fill=COLORS["line"], anchor="mm")

    # title
    w = ev["window"]
    title = f"{ev['event_id']}"
    sub = f"HTF period {local_time(w['start'], ev.get('time_zone'))} to {local_time(w['end'], ev.get('time_zone'))}"
    d.rectangle([0, 0, W, 62], fill=(255, 255, 255, 235))
    d.text((16, 8), title, font=font(22), fill=(20, 20, 20))
    d.text((16, 36), sub, font=font(16), fill=(70, 70, 70))

    # legend
    lx, ly = 16, 80
    items = [
        ("htf", f"HTF point {ev['htf_id']}  ({lat:.4f}, {lon:.4f})",
         f"threshold {ev['threshold_ft_mhhw']:.2f} ft ({ev['threshold_m_mhhw']:.3f} m) above MHHW"),
        ("radius", f"{radius_km:g} km camera-search radius", None),
        (cam["source"], f"{SOURCE_NAME.get(cam['source'], cam['source'])}: {cam['id']}", (cam.get("name") or "")[:52]),
        ("line", f"Distance from HTF point to camera: {dist}", None),
    ]
    box_h = 20 + sum(46 if s else 30 for _, _, s in items)
    d.rounded_rectangle([lx, ly, lx + 520, ly + box_h], 8, fill=(255, 255, 255, 240), outline=(150, 150, 150))
    y = ly + 14
    for kind, text, sub_text in items:
        sym = (lx + 26, y + 11)
        if kind == "htf":
            htf_marker(d, sym)
        elif kind == "radius":
            dashed_circle(d, sym, 11, COLORS["radius"], width=3, dash_deg=30)
        elif kind == "line":
            d.line([(sym[0] - 14, sym[1]), (sym[0] + 14, sym[1])], fill=COLORS["line"], width=4)
        else:
            marker(d, sym, kind, size=10)
        d.text((lx + 52, y), text, font=font(17), fill=(20, 20, 20))
        if sub_text:
            d.text((lx + 52, y + 21), sub_text, font=font(14), fill=(90, 90, 90))
        y += 46 if sub_text else 30

    # scale bar (1 or 2 km) and north arrow
    mpp = metres_per_px(lat, z)
    km = 2 if 2000 / mpp < 260 else 1
    bar = km * 1000 / mpp
    bx, by = W - 40 - bar, H - 60
    d.rectangle([bx - 10, by - 30, W - 30, by + 16], fill=(255, 255, 255, 230))
    d.rectangle([bx, by, bx + bar, by + 8], fill=(30, 30, 30))
    d.rectangle([bx + bar / 2, by, bx + bar, by + 8], fill="white", outline=(30, 30, 30))
    d.text((bx, by - 24), f"0      {km / 2:g}      {km} km", font=font(14), fill=(30, 30, 30))
    nx, ny = W - 50, 100
    d.polygon([(nx, ny - 26), (nx + 12, ny + 8), (nx, ny), (nx - 12, ny + 8)], fill=(30, 30, 30))
    d.text((nx, ny + 22), "N", font=font(18), fill=(30, 30, 30), anchor="mm")

    # attribution
    d.text((W - 10, H - 8), ATTRIBUTION, font=font(12), fill=(60, 60, 60), anchor="rd")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path


def main():
    """Add map.png to every camera folder that has frames but no map yet."""
    from .capture import safe
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", required=True)
    ap.add_argument("--force", action="store_true", help="redraw existing maps")
    args = ap.parse_args()
    made = 0
    for p in sorted(Path(args.events).glob("*/*/event.json")):
        ev = json.loads(p.read_text())
        # Match camera folders on disk (they may hold frames event.json no longer lists).
        by_folder = {f"{c['source']}_{safe(c['id'])}": c for c in ev["cameras"]}
        for folder in sorted(d for d in p.parent.iterdir() if d.is_dir() and any(d.glob("*.jpg"))):
            cam = by_folder.get(folder.name)
            out = folder / "map.png"
            if cam and (args.force or not out.exists()) and render(ev, cam, out):
                made += 1
    print(f"drew {made} maps")


if __name__ == "__main__":
    main()
