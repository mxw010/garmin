#!/usr/bin/env python3
"""
Pull Garmin Connect activities (list + original .FIT files) with the Personal API.

Setup (WSL):
    pip install requests
    export GARMIN_API_KEY='your-personal-api-key'      # never hard-code it

Run:
    python3 garmin_pull.py --list-only                 # 1) just list, see how far back it goes
    python3 garmin_pull.py                             # 2) list + download every .FIT file
    python3 garmin_pull.py --start 2021-01-01 --end 2026-11-01 --out garmin

Output:
    garmin/activities.jsonl   one raw activity summary per line (deduplicated by activityId)
    garmin/activities.tsv     id, start time, type, name, distance_km, duration_min, has_gps
    garmin/fit/<id>.fit       original files (skipped if already downloaded, so it is safe to re-run)
"""
import argparse, datetime as dt, io, json, os, sys, time, zipfile
import requests

BASE = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
LIST_PATH = "/activitylist-service/activities/search/activities"
FIT_PATH = "/download-service/files/activity/{id}"
PAGE = 100          # Garmin errors on very large pages; 100 per request is safe
PAUSE = 0.5         # seconds between requests, to be polite


class Garmin:
    def __init__(self, key):
        self.key, self.token, self.exp = key, None, 0
        self.s = requests.Session()

    def _token(self):
        if self.token and time.time() < self.exp:
            return self.token
        r = self.s.post(BASE + TOKEN_PATH, headers={"Authorization": f"ApiKey {self.key}", "Accept": "*/*"}, timeout=30)
        if not r.ok:
            sys.exit(f"Token request failed: HTTP {r.status_code}. Check that GARMIN_API_KEY is valid and not expired.")
        body = r.text.strip()
        if body.startswith("{"):
            j = r.json()
            self.token = j.get("access_token") or j.get("accessToken") or j.get("token")
            ttl = int(j.get("expires_in") or j.get("expiresIn") or 0)
        else:
            self.token, ttl = body.strip('"'), 0
        if not self.token:
            sys.exit(f"Token response had no token. Fields: {list(r.json().keys())}")
        self.exp = time.time() + (ttl - 60 if ttl > 120 else 23 * 3600)
        return self.token

    def get(self, path, params=None, accept="application/json"):
        for attempt in range(6):
            r = self.s.get(BASE + path, params=params, timeout=60,
                           headers={"Authorization": f"Bearer {self._token()}", "Accept": accept})
            if r.status_code == 401:                       # token expired: get a new one and retry
                self.token = None; continue
            if r.status_code == 406 and accept != "*/*":
                accept = "*/*"; continue
            if r.status_code in (429, 500, 502, 503, 504):   # rate limited / transient: back off
                wait = int(r.headers.get("Retry-After", 0)) or min(60, 2 ** attempt * 2)
                print(f"  HTTP {r.status_code} on {path}, waiting {wait}s", file=sys.stderr)
                time.sleep(wait); continue
            r.raise_for_status()
            time.sleep(PAUSE)
            return r
        r.raise_for_status()


def month_windows(start, end):
    """Calendar-month windows [a, b], so no single query asks for years at once."""
    a = start
    while a <= end:
        nxt = (a.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        yield a, min(nxt - dt.timedelta(days=1), end)
        a = nxt


def list_activities(g, start, end):
    seen = {}
    for a, b in month_windows(start, end):
        got = 0
        for off in range(0, 10_000, PAGE):
            page = g.get(LIST_PATH, {"start": off, "limit": PAGE,
                                     "startDate": a.isoformat(), "endDate": b.isoformat()}).json()
            if isinstance(page, dict):                      # some responses wrap the list
                page = next((v for v in page.values() if isinstance(v, list)), [])
            for act in page:
                seen[act["activityId"]] = act
            got += len(page)
            if len(page) < PAGE:
                break
        print(f"{a:%Y-%m}: {got:3d} activities", file=sys.stderr)
    return sorted(seen.values(), key=lambda x: x.get("startTimeLocal") or x.get("startTimeGMT") or "")


def save_fit(g, act_id, folder):
    path = os.path.join(folder, f"{act_id}.fit")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return "skip"
    data = g.get(FIT_PATH.format(id=act_id), accept="*/*").content
    if data[:2] == b"PK":                                   # downloads usually come zipped
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            fits = [n for n in z.namelist() if n.lower().endswith(".fit")]
            if not fits:
                return "no-fit"
            data = z.read(fits[0])
    with open(path + ".part", "wb") as f:
        f.write(data)
    os.replace(path + ".part", path)
    return "ok"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--end", default=dt.date.today().isoformat())
    p.add_argument("--out", default="garmin")
    p.add_argument("--list-only", action="store_true")
    a = p.parse_args()
    key = os.environ.get("GARMIN_API_KEY")
    if not key:
        sys.exit("Set GARMIN_API_KEY first:  export GARMIN_API_KEY='...'")
    g = Garmin(key)
    os.makedirs(os.path.join(a.out, "fit"), exist_ok=True)

    acts = list_activities(g, dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end))
    with open(os.path.join(a.out, "activities.jsonl"), "w") as f:
        for x in acts:
            f.write(json.dumps(x) + "\n")
    with open(os.path.join(a.out, "activities.tsv"), "w") as f:
        f.write("activityId\tstart\ttype\tname\tdistance_km\tduration_min\thas_gps\n")
        for x in acts:
            typ = (x.get("activityType") or {}).get("typeKey", "")
            gps = x.get("hasPolyline", x.get("startLatitude") is not None)
            f.write(f"{x['activityId']}\t{x.get('startTimeLocal','')}\t{typ}\t{(x.get('activityName') or '').replace(chr(9),' ')}\t"
                    f"{(x.get('distance') or 0)/1000:.2f}\t{(x.get('duration') or 0)/60:.1f}\t{gps}\n")

    years = {}
    for x in acts:
        years[(x.get("startTimeLocal") or "????")[:4]] = years.get((x.get("startTimeLocal") or "????")[:4], 0) + 1
    print(f"\n{len(acts)} activities. Earliest: {acts[0].get('startTimeLocal') if acts else 'none'}", file=sys.stderr)
    print("Per year: " + ", ".join(f"{y}: {n}" for y, n in sorted(years.items())), file=sys.stderr)
    if a.list_only:
        return

    stats = {}
    for i, x in enumerate(acts, 1):
        try:
            res = save_fit(g, x["activityId"], os.path.join(a.out, "fit"))
        except requests.HTTPError as e:
            res = f"error {e.response.status_code}"
        stats[res] = stats.get(res, 0) + 1
        print(f"[{i}/{len(acts)}] {x.get('startTimeLocal','')[:10]} {x['activityId']} {res}", file=sys.stderr)
    print(f"\nFIT downloads: {stats}", file=sys.stderr)


if __name__ == "__main__":
    main()
