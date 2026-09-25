# Reviewer guide: checking camera images for high tide flooding

Thank you for helping with this project! This guide explains everything you need to review the camera images. No programming is needed; you only work in one Excel file.

---

## 1. What is this project about?

Our lab runs an app that **forecasts high tide flooding (HTF)** along the US coast.
- **The forecast:** for 1,431 coastal points, the app predicts when the water level will rise above that point's flooding threshold. We call each such period an **HTF period**.
- **The cameras:** whenever the app forecasts flooding, an automated system takes photos from public cameras within 5 km of that point, before, during and after the HTF period. The cameras are traffic cameras, USGS river/coast cameras, and webcams.

**Your job is to look at those photos and tell us whether flooding is actually visible.** Your answers let us measure how often the forecast is right.

---

## 2. What is in this folder

```
TWL_CrossValidation_captures/
├── HTF_camera_review.xlsx      ← the only file you edit
├── Reviewer_Guide.docx         ← this guide
├── 2026-09-24/                 ← one folder per date
│   └── 20260924T22Z_HTF1071_exceedance/     ← one folder per event
│       ├── event.json                        ← technical details (you can ignore it)
│       ├── traffic_VA-cam-2853/              ← one folder per camera
│       │   ├── map.png                       ← map + forecast chart for this camera
│       │   ├── 2026-09-24T21-30Z.jpg         ← photos, named by the time they were taken (UTC)
│       │   └── 2026-09-24T21-45Z.jpg
│       └── windy_1744113729/
│           └── …
└── 2026-09-25/
```

**Please do not rename, move or delete any files or folders.** The system updates them automatically every hour.

---

## 3. The Excel file: `HTF_camera_review.xlsx`

It has three sheets:

| Sheet | What it is |
|---|---|
| **Instructions** | A short version of this guide, with an example row |
| **Review** | Your work list: **one row = one camera for one event** |
| **Progress** | Counts how many rows you have finished |

### Columns on the Review sheet

| Column | Meaning |
|---|---|
| Event ID | Name of the event, e.g. `20260924T22Z_HTF1071_exceedance` (it matches the event folder name) |
| HTF ID, latitude, longitude | The forecast point on the coast |
| Time zone | Local time zone of that point, e.g. `America/New_York` |
| **HTF period start / end (local)** | **When the app forecast flooding, in local time. This is the most important time window.** |
| HTF period start / end (UTC) | The same window in UTC |
| Camera source | Traffic camera (state DOT), USGS river/coast camera, or Windy webcam |
| Camera ID, Camera name | Which camera took the photos |
| Distance to HTF point (km) | How far the camera is from the forecast point |
| Number of images | How many photos this camera has for the event |
| First / Last image (local) | Time of the first and last photo, in local time |
| Old images to ignore | Photos that were already more than 30 minutes old when the system saved them (frozen or slow cameras). Judge by the other photos. |
| **Image folder** | **Click to open this camera's photos** |
| **Location map** | **Click to open `map.png`** (see section 5) |
| **Has the image flooded?** | **Yellow: your answer** |
| **Notes (optional)** | **Yellow: your short comment** |

**Only edit the two yellow columns.** Everything else is filled in automatically.

---

## 4. How to review one row, step by step

1. **Read the HTF period** (local time) on the row, e.g. *2026-09-24 18:00 to 20:00*.
2. **Click "Open map"** to see where the camera is, relative to the forecast point, and when its photos were taken (section 5).
3. **Click the image folder link** and open the photos.
   - Photo names are in **UTC**, e.g. `2026-09-24T23-15Z.jpg` = 23:15 UTC.
   - On the US East Coast in summer (EDT), local time = UTC − 4 hours, so 23:15 UTC = 19:15 EDT. The chart in `map.png` already shows local times.
4. **Compare the photos over time.** Look at photos before, during and after the HTF period. Tidal flooding rises slowly and then drains away, so changes over the period are the best evidence.
5. **Choose your answer** from the drop-down in *Has the image flooded?* (next section).
6. **Add a note if it helps**, e.g. *"Water across Shore Dr at 19:15, gone by 20:30"*.

---

## 5. The map and chart (`map.png`)

Each camera folder has a `map.png` with two panels.

**(a) Map (top)**
- **Red circle:** the forecast (HTF) point.
- **Dashed blue circle:** the 5 km search area.
- **Colored marker:** the camera.
  - Blue square: traffic camera.
  - Teal circle: USGS camera.
  - Purple triangle: Windy webcam.
- **Black line:** the distance from the forecast point to the camera.

**(b) Chart (bottom)**
- **Blue line:** the forecast water level.
- **Purple dashed line:** the flooding threshold.
- **Red shading:** the time the forecast is above the threshold.
- **Orange band:** the HTF period.
- **Green lines:** the times this camera took photos.

Use the chart to find the photos taken **during** the orange band.

---

## 6. Choosing your answer

Pick exactly one option from the drop-down:

| Answer | Use it when… | Examples |
|---|---|---|
| **Yes** | You can see **coastal flooding**: seawater on land that is normally dry | Water covering a road, sidewalk, parking lot or yard; water over a seawall, bulkhead or dock; standing water that is not in the same camera's photos at other times |
| **No** | The photos are clear, and there is **no flooding** | Water stays in its normal channel, bay or beach; waves on the sand that don't reach roads or buildings; a road that is only wet from rain (no standing or moving water) |
| **NaN** | You **cannot judge** from the photos | All photos are too dark (night), foggy, blurred, have raindrops on the lens, are missing or broken, or don't cover the HTF period |
| **Invalid** | The camera **cannot show ground-level flooding at all** | The camera is on a bridge or overpass high above the ground; it points at the sky, a highway far from the water, or somewhere the water could never reach |

**Tips**
- **Judge each camera by its own photos.** One event can have several cameras, and one may show flooding (Yes) while another looks away from it (No or Invalid).
- **Rain vs. tidal flooding:** rain makes the whole road shiny and wet everywhere. Tidal flooding makes standing or slowly moving water that grows during the HTF period and then goes away.
- **When in doubt between Yes and No,** choose the one you believe more likely and explain in Notes. Use **NaN** only when the photos really cannot be judged.
- **Always add a short note** for NaN and Invalid (e.g. *"all photos at night"*, *"camera on bridge"*).

---

## 7. Saving your work

- **Save often** (Ctrl+S / Cmd+S), and keep the file format **.xlsx**.
- **Close Excel when you finish a session.** Every hour the system adds rows for new events to this file. It never removes your answers, but it can only add new rows while the file is closed.
- **New rows** can appear at the bottom (or, after sorting, anywhere). Use the filter on *Has the image flooded?* and choose **(Blanks)** to see only the rows you still need to do.
- **If a file named like "HTF_camera_review (conflict …).xlsx" appears,** do not delete it. Tell your supervisor.

---

## 8. Checking your progress

The **Progress** sheet shows:
- how many rows there are;
- how many are answered Yes, No, NaN and Invalid;
- how many are still blank;
- the percentage done.

---

## 9. Quick reference

| | |
|---|---|
| File to edit | `HTF_camera_review.xlsx`, **Review** sheet, yellow columns only |
| Time to focus on | **HTF period (local)**, the orange band in `map.png` |
| Photo names | UTC time, e.g. `…T23-15Z.jpg` = 23:15 UTC |
| Answers | **Yes** (flooded) · **No** (not flooded) · **NaN** (can't see) · **Invalid** (camera not usable) |
| Don't | Rename, move or delete files; edit non-yellow columns |
| Questions | Ask your supervisor, Ehsan Kahrizi (Coastal Hydrology Lab) |
