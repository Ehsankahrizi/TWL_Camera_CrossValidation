"""Location map + forecast chart (map.png) for each camera folder.

Top panel: the HTF point, the camera-search radius, the camera, a line between them
with the distance, a legend, a scale bar and a north arrow, on an Esri street basemap
(Pillow, web-mercator tiles). Bottom panel (same width, wide and short): the mean NWM TWL forecast for the HTF point
(ft above MHHW) with the threshold, the part above it shaded as in the iOS app, the
HTF period, earlier forecast runs, and a marker at every image this camera captured.

Usage (existing folders):  python -m crossval.maps --events "<folder with <date>/<event_id>/event.json>"
"""

import argparse
import io
import json
import math
import os
import re
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
TILE_CACHE = os.environ.get("TILE_CACHE", "/tmp/crossval_tiles")   # reused across cycles


FONT_FAMILY = ["Arial", "Liberation Sans", "Helvetica", "DejaVu Sans"]   # Liberation Sans = Arial metrics
_fonts = {}


def font(size, bold=False):
    """Same typeface as the chart (Arial, or Liberation Sans in the container)."""
    key = (size, bold)
    if key not in _fonts:
        try:
            from matplotlib import font_manager as fm
            path = fm.findfont(fm.FontProperties(family=FONT_FAMILY, weight="bold" if bold else "normal"))
            _fonts[key] = ImageFont.truetype(path, size)
        except Exception:
            _fonts[key] = ImageFont.load_default(size=size)
    return _fonts[key]


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
        cached = Path(TILE_CACHE) / f"{z}/{x}/{y}.png"
        data = cached.read_bytes() if cached.exists() else None
        if data is None:
            r = http_get(TILE_URL.format(z=z, x=x, y=y), timeout=20, retries=2)
            data = r.content if r is not None and r.ok else None
            if data:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(data)
        try:
            _tiles[key] = Image.open(io.BytesIO(data)).convert("RGB") if data else None
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


CHART_H = 600                        # bottom panel height (px); width = map width
CHART_STYLE = {
    "font.family": "sans-serif", "font.sans-serif": FONT_FAMILY, "font.size": 12,
    "axes.titlesize": 15, "axes.titleweight": "bold", "axes.labelsize": 14, "axes.labelweight": "bold",
    "axes.linewidth": 1.3, "xtick.labelsize": 12, "ytick.labelsize": 12,
    "xtick.direction": "in", "ytick.direction": "in", "xtick.top": True, "ytick.right": True,
    "xtick.major.size": 6, "ytick.major.size": 6, "xtick.major.width": 1.2, "ytick.major.width": 1.2,
    "legend.fontsize": 12, "grid.linewidth": 0.6,
}

def chart(ev, cam, image_times, width, height):
    """Bottom panel: forecast time series with threshold, HTF period and image times."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    tz = None
    try:
        tz = ZoneInfo(ev["time_zone"]) if ev.get("time_zone") else None
    except Exception:
        pass
    to_dt = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    thr = ev["threshold_ft_mhhw"]
    plt.rcParams.update(CHART_STYLE)
    fig, ax = plt.subplots(figsize=(width / 100, height / 100), dpi=100)
    runs = [f for f in ev["forecasts"] if f.get("series_ft_mhhw")]
    for f in runs[:-1]:                                                 # earlier runs, faint
        s = f["series_ft_mhhw"]
        ax.plot([to_dt(p["t"]) for p in s], [p["v"] for p in s], color="0.6", lw=1, alpha=0.6,
                label="Earlier forecast runs" if f is runs[0] else None)
    if runs:
        s = runs[-1]["series_ft_mhhw"]
        t, v = [to_dt(p["t"]) for p in s], [p["v"] for p in s]
        ax.fill_between(t, v, thr, where=[x >= thr for x in v], interpolate=True, color="#dc2626", alpha=0.35,
                        label="Forecast above threshold")
        ax.plot(t, v, color="#2563eb", lw=2.6, marker="o", ms=5, label="Mean NWM TWL forecast (latest run)")
    w = ev["window"]
    ax.axvspan(to_dt(w["start"]), to_dt(w["end"]) if w["end"] != w["start"] else to_dt(w["end"]) + (to_dt(w["end"]) - to_dt(w["start"])),
               color="#f59e0b", alpha=0.15, label="HTF period (forecast ≥ threshold)")
    ax.axhline(thr, color="#9333ea", ls="--", lw=2.2, label=f"HTF threshold ({thr:.2f} ft)")
    # y range from every plotted value and the threshold (explicit, so nothing is clipped)
    vals = [p["v"] for f in runs for p in f["series_ft_mhhw"]] + [thr]
    lo, hi = min(vals), max(vals)
    pad = max(0.1, (hi - lo) * 0.12)
    ax.set_ylim(lo - pad, hi + pad * 1.6)
    times = sorted(set(image_times))
    fmt = lambda d: d.astimezone(tz).strftime("%H:%M") if tz else d.strftime("%H:%M")
    for i, it in enumerate(times):
        ax.axvline(it, color="#15803d", lw=1.5, alpha=0.9, label=f"Image captured (n = {len(times)})" if i == 0 else None)
    if times:
        top = hi + pad * 1.6
        ax.plot(times, [top] * len(times), "v", color="#15803d", ms=10, clip_on=False)
        xs = [to_dt(p["t"]) for f in runs for p in f["series_ft_mhhw"]] + times
        span = (max(xs) - min(xs)).total_seconds() or 1
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        if len(times) <= 4 and all(g > span / 10 for g in gaps):   # few, well apart: label each
            for it in times:
                ax.annotate(fmt(it), (it, top), xytext=(0, 10), textcoords="offset points",
                            ha="center", fontsize=11, fontweight="bold", color="#15803d")
        else:                                                # many: one summary box
            ax.annotate(f"{len(times)} images: {fmt(times[0])}–{fmt(times[-1])}", (times[len(times) // 2], top),
                        xytext=(0, 10), textcoords="offset points", ha="center", fontsize=11, fontweight="bold",
                        color="#15803d", bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#15803d", lw=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M", tz=tz))
    zone = datetime.now(tz).strftime("%Z") if tz else "UTC"
    ax.set_xlabel(f"Time ({zone}, local to the HTF point)")
    ax.set_ylabel("TWL (ft above MHHW)")
    run = runs[-1].get("nwm_creation_time", "")[:16].replace("T", " ") if runs else ""
    n_st = len(ev.get("nwm_stations") or [])
    ax.set_title(f"(b) NWM total water level forecast at HTF point {ev['htf_id']}", loc="left",
                 pad=30 if times else 12)
    ax.set_title(f"NWM run {run} UTC · mean of {n_st} station{'s' if n_st != 1 else ''} ≤ 5 km", loc="right",
                 fontsize=11, fontweight="normal", color="0.3", pad=30 if times else 12)
    ax.grid(alpha=0.35)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=3, frameon=False,
              handlelength=2.2, columnspacing=1.6)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    buf.seek(0)
    img = Image.open(buf).convert("RGB")
    if img.width != width:                                   # tight bbox may change the size slightly
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    return img


def render(ev, cam, out_path, image_times=None):
    """Draw map.png (map + forecast chart) for one camera of one event.
    image_times: datetimes of this camera's images. Returns the path or None on failure."""
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
    tb = d.textbbox(mid, dist, font=font(22, bold=True), anchor="mm")
    d.rounded_rectangle([tb[0] - 9, tb[1] - 6, tb[2] + 9, tb[3] + 6], 6, fill=(255, 255, 255, 240), outline=COLORS["line"], width=2)
    d.text(mid, dist, font=font(22, bold=True), fill=COLORS["line"], anchor="mm")

    # title
    w = ev["window"]
    title = f"{ev['event_id']}"
    sub = f"HTF period {local_time(w['start'], ev.get('time_zone'))} to {local_time(w['end'], ev.get('time_zone'))}"
    d.rectangle([0, 0, W, 74], fill=(255, 255, 255, 240))
    d.text((16, 8), f"(a) {title}", font=font(24, bold=True), fill=(15, 15, 15))
    d.text((16, 42), sub, font=font(18), fill=(60, 60, 60))

    # legend
    lx, ly = 16, 92
    items = [
        ("htf", f"HTF point {ev['htf_id']}  ({lat:.4f}, {lon:.4f})",
         f"threshold {ev['threshold_ft_mhhw']:.2f} ft ({ev['threshold_m_mhhw']:.3f} m) above MHHW"),
        ("radius", f"{radius_km:g} km camera-search radius", None),
        (cam["source"], f"{SOURCE_NAME.get(cam['source'], cam['source'])}: {cam['id']}", (cam.get("name") or "")[:52]),
        ("line", f"Distance from HTF point to camera: {dist}", None),
    ]
    box_h = 22 + sum(52 if s else 34 for _, _, s in items)
    d.rounded_rectangle([lx, ly, lx + 600, ly + box_h], 8, fill=(255, 255, 255, 242), outline=(120, 120, 120), width=2)
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
        d.text((lx + 54, y), text, font=font(19, bold=True), fill=(20, 20, 20))
        if sub_text:
            d.text((lx + 54, y + 24), sub_text, font=font(16), fill=(80, 80, 80))
        y += 52 if sub_text else 34

    # scale bar (1 or 2 km) and north arrow
    mpp = metres_per_px(lat, z)
    km = 2 if 2000 / mpp < 260 else 1
    bar = km * 1000 / mpp
    bx, by = W - 60 - bar, H - 62
    d.rectangle([bx - 22, by - 36, W - 22, by + 20], fill=(255, 255, 255, 235))
    d.rectangle([bx, by, bx + bar, by + 10], fill=(30, 30, 30))
    d.rectangle([bx + bar / 2, by, bx + bar, by + 10], fill="white", outline=(30, 30, 30), width=2)
    for frac, lab in ((0, "0"), (0.5, f"{km / 2:g}"), (1, f"{km} km")):
        d.text((bx + bar * frac, by - 6), lab, font=font(16, bold=True), fill=(30, 30, 30), anchor="mb")
    nx, ny = W - 50, 112
    d.polygon([(nx, ny - 28), (nx + 13, ny + 9), (nx, ny), (nx - 13, ny + 9)], fill=(30, 30, 30))
    d.text((nx, ny + 26), "N", font=font(20, bold=True), fill=(30, 30, 30), anchor="mm")

    # attribution
    d.text((W - 10, H - 6), ATTRIBUTION, font=font(12), fill=(60, 60, 60), anchor="rd")

    panel = chart(ev, cam, image_times or [], W, CHART_H)
    both = Image.new("RGB", (W, H + panel.height), "white")
    both.paste(img, (0, 0))
    both.paste(panel, (0, H))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    both.save(out_path, "PNG", optimize=True)
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
            if not cam or not (args.force or not out.exists()):
                continue
            times = {datetime.fromisoformat(re.sub(r"T(\d\d)-(\d\d)Z$", r"T\1:\2:00+00:00", f.stem))
                     for f in folder.glob("*.jpg")}
            if render(ev, cam, out, sorted(times)):
                made += 1
    print(f"drew {made} maps")


if __name__ == "__main__":
    main()
