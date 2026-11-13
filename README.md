# garmin

Download your Garmin Connect activities and draw them all on one interactive map.

- `garmin_pull.py` lists your activities through the Garmin Personal API and downloads the original `.FIT` file for each one.
- `garmin_heatmap.py` reads those `.FIT` files and writes a single `heatmap.html` with every GPS track on it.

## Setup

Python 3 and three packages:

```bash
pip install requests fitdecode folium
```

The pull script reads your Garmin Personal API key from the environment. Do not put it in the code.

```bash
export GARMIN_API_KEY='your-personal-api-key'
```

## 1. Pull activities

```bash
python3 garmin_pull.py --list-only     # list only, to see how far back your history goes
python3 garmin_pull.py                 # list, then download every .FIT file
python3 garmin_pull.py --start 2021-01-01 --end 2026-11-01 --out garmin
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--start` | `2021-01-01` | first day to fetch |
| `--end` | today | last day to fetch |
| `--out` | `garmin` | output folder |
| `--list-only` | off | write the activity lists but skip the `.FIT` downloads |

Output:

| File | Contents |
| --- | --- |
| `garmin/activities.jsonl` | one raw activity summary per line, deduplicated by activity ID |
| `garmin/activities.tsv` | ID, start time, type, name, distance (km), duration (min), has GPS |
| `garmin/fit/<id>.fit` | the original activity files |

Files that are already downloaded are skipped, so the script is safe to re-run. It queries one calendar month at a time, pauses between requests, and backs off when Garmin rate-limits it.

## 2. Build the map

```bash
python3 garmin_heatmap.py
python3 garmin_heatmap.py --trim 60 --out heatmap.html
```

Then open `heatmap.html` in a browser (from WSL: `explorer.exe heatmap.html`).

What it does:

- Keeps GPS points only; activities without GPS (indoor ones) are skipped.
- Removes the first and last `--trim` seconds of each activity, so start and end points at home are not shown.
- Thins each track to one point every `--step` metres to keep the page fast.
- Breaks a track where consecutive points jump more than 500 m (GPS loss, a car shuttle), so no straight lines cross the map.
- Draws one line per activity, coloured by type: cycling, skiing, hiking, climbing, running, walking, other. Each type is a layer you can toggle, and hovering a line shows its date, type, distance and name.
- Adds an optional density heatmap layer and four base maps: roads, OpenStreetMap, topo and satellite.

| Option | Default | Meaning |
| --- | --- | --- |
| `--fit` | `garmin/fit` | folder of `.FIT` files |
| `--tsv` | `garmin/activities.tsv` | activity list, used for names, types and distances |
| `--trim` | `60` | seconds removed at the start and end of each activity |
| `--step` | `15` | keep one point every N metres |
| `--out` | `heatmap.html` | output file |
| `--width` | `4` | line width in pixels |
| `--opacity` | `0.5` | line opacity, 0 to 1 |
| `--view` | `core` | `core` opens on where most activities are; `all` fits every track, including far-away trips |

Notes:

- The map can get large with a long history. If it is slow, rerun with a bigger `--step` (for example `--step 30`).
- The OpenStreetMap base layer only loads when the page is served over HTTP (for example `python3 -m http.server`), not when it is opened straight from disk. The default roads layer works either way.

## Privacy

Your activity data and the generated map contain your GPS tracks. `garmin/` and `heatmap.html` are in `.gitignore` so they are not committed.

## License

MIT. See [LICENSE](LICENSE).
