#!/usr/bin/env python3
"""
Interactive heatmap of Garmin activities on an OpenStreetMap base map.

Install (WSL):
    pip install fitdecode folium

Run (after garmin_pull.py):
    python3 garmin_heatmap.py                      # reads garmin/fit + garmin/activities.tsv
    python3 garmin_heatmap.py --trim 60 --out heatmap.html
Then open heatmap.html in a browser (from WSL: explorer.exe heatmap.html).

What it does:
  * reads every .FIT file, keeps GPS points only (indoor activities are skipped)
  * drops the first and last --trim seconds of each activity (default 60 s) by timestamp,
    so start/end points at home are not shown
  * thins each track to one point every --step metres (default 15 m) to keep the HTML fast
  * draws faint lines per activity type (toggle types in the layer menu, top right),
    plus an optional density heatmap layer and three base maps (street, topo, satellite)
"""
import argparse, csv, glob, math, os, sys
from collections import defaultdict
import fitdecode
import folium
from folium.plugins import HeatMap, Fullscreen

SEMI = 180.0 / 2**31          # FIT stores lat/lon as "semicircles"

# activity type -> (group label, color). Groups keep the legend short.
GROUPS = [
    ("cycling",  "Cycling",           "#2a78d6"),
    ("biking",   "Cycling",           "#2a78d6"),
    ("ski",      "Skiing",            "#00a3bf"),
    ("snowboard","Skiing",            "#00a3bf"),
    ("hik",      "Hiking",            "#e37b1c"),
    ("mountaineering", "Hiking",      "#e37b1c"),
    ("climb",    "Climbing",          "#8f4bd6"),
    ("run",      "Running",           "#d6336c"),
    ("walk",     "Walking",           "#5f8f2a"),
]
OTHER = ("Other", "#6b7280")


def group_of(type_key):
    t = (type_key or "").lower()
    for k, label, color in GROUPS:
        if k in t:
            return label, color
    return OTHER


def haversine(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def read_fit(path):
    """Return (sport, [(timestamp, lat, lon), ...]) from one FIT file."""
    pts, sport = [], None
    with fitdecode.FitReader(path, check_crc=fitdecode.CrcCheck.DISABLED) as fr:
        for frame in fr:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "record" and frame.has_field("position_lat") and frame.has_field("position_long"):
                lat, lon, ts = frame.get_value("position_lat"), frame.get_value("position_long"), frame.get_value("timestamp", fallback=None)
                if lat is None or lon is None or ts is None:
                    continue
                pts.append((ts, lat * SEMI, lon * SEMI))
            elif frame.name in ("session", "sport") and sport is None and frame.has_field("sport"):
                sport = str(frame.get_value("sport"))
    return sport, pts


def trim_and_thin(pts, trim_s, step_m):
    if len(pts) < 2:
        return []
    t0, t1 = pts[0][0], pts[-1][0]
    if (t1 - t0).total_seconds() <= 2 * trim_s:
        return []                                    # too short to show anything after trimming
    keep = [p for p in pts if (p[0] - t0).total_seconds() >= trim_s and (t1 - p[0]).total_seconds() >= trim_s]
    out = []
    for _, lat, lon in keep:
        if not out or haversine(out[-1], (lat, lon)) >= step_m:
            out.append((lat, lon))
    return out


def split_gaps(track, gap_m=500):
    """Break a track where consecutive points jump > gap_m (GPS loss, car shuttle) so no straight lines cross the map."""
    segs, cur = [], []
    for p in track:
        if cur and haversine(cur[-1], p) > gap_m:
            if len(cur) > 1: segs.append(cur)
            cur = []
        cur.append(p)
    if len(cur) > 1: segs.append(cur)
    return segs


def load(folder, tsv, trim_s, step_m):
    meta = {}
    if os.path.exists(tsv):
        with open(tsv, encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter="\t"):
                meta[r["activityId"]] = r
    tracks, skipped = [], defaultdict(int)
    files = sorted(glob.glob(os.path.join(folder, "*.fit")))
    for i, path in enumerate(files, 1):
        aid = os.path.splitext(os.path.basename(path))[0]
        try:
            sport, pts = read_fit(path)
        except Exception as e:                       # a corrupt file should not stop the whole map
            skipped["unreadable"] += 1
            print(f"  skip {aid}: {e}", file=sys.stderr)
            continue
        if not pts:
            skipped["no GPS"] += 1
            continue
        tr = trim_and_thin(pts, trim_s, step_m)
        if len(tr) < 2:
            skipped["too short after trim"] += 1
            continue
        m = meta.get(aid, {})
        label, color = group_of(m.get("type") or sport)
        try:
            km = float(m.get("distance_km") or 0)                 # Garmin's own total, full activity
        except ValueError:
            km = 0
        if km <= 0:                                               # fall back to the GPS track itself
            km = sum(haversine(pts[j - 1][1:], pts[j][1:]) for j in range(1, len(pts))) / 1000
        tracks.append(dict(id=aid, name=m.get("name", ""), date=(m.get("start") or str(pts[0][0]))[:10],
                           type=m.get("type") or sport or "", group=label, color=color, km=km, segs=split_gaps(tr)))
        if i % 100 == 0:
            print(f"  read {i}/{len(files)}", file=sys.stderr)
    return tracks, dict(skipped), len(files)


def build_map(tracks, out, line_opacity=0.5, line_weight=4, view="core"):
    allpts = [p for t in tracks for s in t["segs"] for p in s]
    lats = sorted(p[0] for p in allpts); lons = sorted(p[1] for p in allpts)
    center = (lats[len(lats) // 2], lons[len(lons) // 2])          # median point = where you are most
    m = folium.Map(location=center, zoom_start=11, tiles=None, prefer_canvas=True, control_scale=True)
    # key-free base maps (CartoDB now needs an API key)
    # OpenStreetMap's own tile server answers 403 to pages opened from disk (no Referer), so the default
    # road map is Esri's; OSM stays as an option for when the page is served over http (see notes below).
    folium.TileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
                     name="Roads", attr="Tiles © Esri — Source: Esri, HERE, Garmin, USGS, OpenStreetMap contributors").add_to(m)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap (needs http server)").add_to(m)
    folium.TileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", name="Topo (contours)", max_zoom=17,
                     attr='Map data © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, '
                          'SRTM | Style © <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)').add_to(m)
    folium.TileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                     name="Satellite", attr="Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics").add_to(m)

    by_group = defaultdict(list)
    for t in tracks:
        by_group[t["group"]].append(t)
    for label in sorted(by_group, key=lambda g: -len(by_group[g])):
        ts = by_group[label]
        fg = folium.FeatureGroup(name=f"{label} ({len(ts)})", show=True)
        for t in ts:
            tip = f"{t['date']} · {t['type']} · {t['km']:.1f} km ({t['km'] * 0.621371:.1f} mi) · {t['name']}"
            for s in t["segs"]:
                folium.PolyLine(s, color=t["color"], weight=line_weight, opacity=line_opacity, tooltip=tip).add_to(fg)
        fg.add_to(m)

    heat = folium.FeatureGroup(name="Density heatmap", show=False)
    HeatMap([[a, b] for a, b in allpts[:: max(1, len(allpts) // 300_000)]],
            radius=6, blur=8, min_opacity=0.25, max_zoom=14).add_to(heat)
    heat.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    Fullscreen().add_to(m)
    # "core": open on where most points are (2nd-98th percentile), so one far-away trip does not zoom out to
    # the whole world; the other tracks are still there, just zoom out. "all": open on every track.
    q = (0.02, 0.98) if view == "core" else (0.0, 1.0)
    m.fit_bounds([[lats[int((len(lats) - 1) * q[0])], lons[int((len(lons) - 1) * q[0])]],
                  [lats[int((len(lats) - 1) * q[1])], lons[int((len(lons) - 1) * q[1])]]])
    m.save(out)
    return len(allpts)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fit", default="garmin/fit")
    p.add_argument("--tsv", default="garmin/activities.tsv")
    p.add_argument("--trim", type=float, default=60, help="seconds removed at start and end of each activity")
    p.add_argument("--step", type=float, default=15, help="keep one point every N metres")
    p.add_argument("--out", default="heatmap.html")
    p.add_argument("--width", type=float, default=4, help="line width in pixels")
    p.add_argument("--opacity", type=float, default=0.5, help="line opacity, 0-1")
    p.add_argument("--view", choices=["core", "all"], default="core",
                   help="initial view: core = where most activities are; all = fit every track (e.g. trips abroad)")
    a = p.parse_args()
    tracks, skipped, n = load(a.fit, a.tsv, a.trim, a.step)
    if not tracks:
        sys.exit(f"No GPS tracks found in {a.fit} ({n} files; skipped: {skipped}).")
    npts = build_map(tracks, a.out, a.opacity, a.width, a.view)
    size = os.path.getsize(a.out) / 1e6
    print(f"{len(tracks)} of {n} activities mapped, {npts:,} points, {size:.1f} MB -> {a.out}", file=sys.stderr)
    print(f"skipped: {skipped}", file=sys.stderr)
    far = sorted({t["date"][:4] + " " + t["type"] for t in tracks
                  if not any(-125 < p[1] < -66 and 24 < p[0] < 50 for s_ in t["segs"] for p in s_[:1])})
    if far:
        print(f"{len(far)} activity groups start outside the contiguous US (zoom out or use --view all): "
              + ", ".join(far[:10]) + (" ..." if len(far) > 10 else ""), file=sys.stderr)
    if size > 60:
        print("Large file: rerun with --step 30 to make it lighter.", file=sys.stderr)


if __name__ == "__main__":
    main()
