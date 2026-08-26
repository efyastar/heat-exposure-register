#!/usr/bin/env python3
"""
Build the site exposure dataset the app runs on.

Demo sites are real, public Phoenix locations chosen to span the city's heat
gradient. They stand in for a contractor's job sites; the tool accepts any
coordinates, these just make the demo concrete and reproducible.

Three pulls, all cached to disk so the app never calls the API at runtime:

  1. exceedance over a summer window   -> ranks sites (historical layers carry
     real spatial spread, ~0.9 C across the city)
  2. hourly temperature on one hot day -> shape of the day, for work/rest windows
  3. today's forecast, walked forward until the API returns empty (~7h)

RESUMABLE. Every completed piece is written to site_data.json immediately, and
re-running skips whatever is already there. A dropped connection costs you one
call, not the whole run.

    python3 build_site_data.py          # run, or resume
    FRESH=1 python3 build_site_data.py  # ignore checkpoint, start over
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from shapely.geometry import Point, shape

load_dotenv()
from fortyguard import FortyGuardClient  # noqa: E402

PHOENIX_TZ = ZoneInfo("America/Phoenix")
OUT_PATH = os.environ.get("OUT", "site_data.json")
GRAN = int(os.environ.get("GRAN", "100"))
FRESH = os.environ.get("FRESH", "") == "1"

# Generous HTTP timeout: the default 60s is not enough to submit a heavy job.
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "180"))
ATTEMPTS = int(os.environ.get("ATTEMPTS", "4"))

# 38C=100F, 40C=104F. 43C was dropped: it returned negative hours, an
# interpolation artefact past the edge of the data.
THRESHOLDS_C = [38.0, 40.0]
WINDOW_START = os.environ.get("WINDOW_START", "2026-07-20")
WINDOW_END = os.environ.get("WINDOW_END", "2026-08-19")
PROFILE_DAY = os.environ.get("PROFILE_DAY", "2026-08-03")

# Twelve real public locations, chosen off the measured grid so the register spans
# the city's full exposure range (~3.6 to ~6.1 dangerous hours a day) rather than
# clustering in the central corridor. Each coordinate was checked against the tile
# it lands in. They stand in for a contractor's job list.
SITES = [
    {"id": "southcentral", "name": "South-central industrial corridor", "lat": 33.4029, "lon": -112.0823},
    {"id": "buckeye",      "name": "Lower Buckeye Road corridor",       "lat": 33.4093, "lon": -112.0696},
    {"id": "university",   "name": "University Park",                   "lat": 33.4181, "lon": -112.0900},
    {"id": "chavez",       "name": "Cesar Chavez Plaza",                "lat": 33.4488, "lon": -112.0775},
    {"id": "grant",        "name": "Grant Park",                        "lat": 33.4398, "lon": -112.0796},
    {"id": "civic",        "name": "Civic Space Park",                  "lat": 33.4524, "lon": -112.0744},
    {"id": "roosevelt",    "name": "Roosevelt Row",                     "lat": 33.4579, "lon": -112.0680},
    {"id": "hance",        "name": "Margaret T. Hance Park",            "lat": 33.4623, "lon": -112.0745},
    {"id": "encanto",      "name": "Encanto Park",                      "lat": 33.4757, "lon": -112.0864},
    {"id": "baselinecorr", "name": "South Phoenix — Baseline corridor", "lat": 33.3866, "lon": -112.0907},
    {"id": "smwest",       "name": "South Mountain Park — west entrance","lat": 33.3724, "lon": -112.0734},
    {"id": "smtrail",      "name": "South Mountain Park — trailhead",   "lat": 33.3752, "lon": -112.0627},
    {"id": "smslope",      "name": "South Mountain Park — upper slope", "lat": 33.3717, "lon": -112.0509},
]

MARGIN = 0.006


def log(msg):
    print(msg, flush=True)


def call_with_retry(fn, label, attempts=ATTEMPTS):
    """Run an API call, retrying with backoff. Returns None if all attempts fail."""
    delay = 5.0
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            name = type(exc).__name__
            if i == attempts:
                log(f"    {label}: gave up after {attempts} attempts ({name})")
                return None
            log(f"    {label}: attempt {i} failed ({name}), retry in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return None


def build_aoi(sites, margin=MARGIN):
    lons = [s["lon"] for s in sites]
    lats = [s["lat"] for s in sites]
    w, e = min(lons) - margin, max(lons) + margin
    s, n = min(lats) - margin, max(lats) + margin
    km2 = (e - w) * 92.5 * (n - s) * 111.0
    log(f"AOI: {w:.4f},{s:.4f} -> {e:.4f},{n:.4f}  (~{km2:.0f} km2, {km2 / 2.59:.0f} mi2)")
    if km2 / 2.59 > 50:
        log("WARNING: AOI exceeds the 50 mi2 premium cap; calls may fail.")
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [[
                [w, s], [e, s], [e, n], [w, n], [w, s]
            ]]},
        }],
    }


def tiles_of(result):
    """Tiles from a response, or []. The API returns empty rather than erroring
    when a request falls outside its data coverage — always check."""
    if not isinstance(result, dict):
        return []
    return (result.get("map_data") or {}).get("features") or []


def assign_tiles(sites, tiles):
    geoms = [(t["properties"]["tile_id"], shape(t["geometry"])) for t in tiles]
    out = {}
    for s in sites:
        pt = Point(s["lon"], s["lat"])
        hit = next((tid for tid, g in geoms if g.contains(pt)), None)
        if hit is None:
            hit = min(geoms, key=lambda tg: tg[1].distance(pt))[0]
        out[s["id"]] = hit
    return out


def values_by_tile(tiles, key):
    return {t["properties"]["tile_id"]: t["properties"].get(key) for t in tiles}


def save(data):
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, OUT_PATH)   # atomic: never leave a half-written file


def load_checkpoint():
    if FRESH or not os.path.exists(OUT_PATH):
        return None
    try:
        with open(OUT_PATH) as fh:
            data = json.load(fh)
        log(f"resuming from {OUT_PATH}: "
            f"{len(data.get('exposure', {}))} exposure layers, "
            f"{len(data.get('hourly', {}))} hours, "
            f"{len(data.get('forecast', {}))} forecast hours")
        return data
    except Exception as exc:
        log(f"checkpoint unreadable ({type(exc).__name__}); starting fresh")
        return None


def main():
    client = FortyGuardClient(timeout=HTTP_TIMEOUT)
    aoi = build_aoi(SITES)

    data = load_checkpoint() or {
        "sites": SITES,
        "window": {"start": WINDOW_START, "end": WINDOW_END},
        "profile_day": PROFILE_DAY,
        "thresholds_c": THRESHOLDS_C,
        "exposure": {}, "hourly": {}, "forecast": {},
        "site_tile": None, "notes": [],
    }
    data["generated_at"] = datetime.now(PHOENIX_TZ).isoformat()
    site_tile = data.get("site_tile")

    # --- 1. exposure -------------------------------------------------------
    log(f"\nexposure: hours above threshold, {WINDOW_START} .. {WINDOW_END}")
    for thr in THRESHOLDS_C:
        key = str(thr)
        if key in data["exposure"]:
            log(f"  >{thr}C  already done, skipping")
            continue

        log(f"  >{thr}C ...")
        res = call_with_retry(
            lambda t=thr: client.create_heatmap(
                polygon_aoi=aoi, granularity=GRAN,
                start_date=WINDOW_START, end_date=WINDOW_END, filter_type=4,
                analytic_type="exceedance", threshold=t, direction="above",
                verbose=False, timeout=900,
            )["result"],
            f">{thr}C",
        )
        tiles = tiles_of(res)
        if not tiles:
            log(f"    >{thr}C returned no tiles — skipping")
            continue

        if site_tile is None:
            site_tile = assign_tiles(SITES, tiles)
            data["site_tile"] = site_tile
            log(f"    mapped {len(site_tile)} sites to tiles")

        vals = values_by_tile(tiles, "value")
        per_site = {sid: vals.get(tid) for sid, tid in site_tile.items()}
        data["exposure"][key] = per_site
        good = [v for v in per_site.values() if v is not None]
        if good:
            log(f"    {len(tiles)} tiles | sites {min(good):.2f}-{max(good):.2f} hours")
        save(data)

    if site_tile is None:
        sys.exit("ERROR: no exposure layer succeeded. Re-run to retry, or shorten "
                 "the window with WINDOW_START/WINDOW_END.")

    # --- 2. hourly profile -------------------------------------------------
    log(f"\nhourly profile for {PROFILE_DAY}")
    for hour in range(24):
        hkey = f"{hour:02d}"
        if hkey in data["hourly"]:
            continue

        res = call_with_retry(
            lambda h=hour: client.create_heatmap(
                polygon_aoi=aoi, granularity=GRAN,
                start_date=PROFILE_DAY, start_time=f"{h:02d}:00",
                filter_type=1, verbose=False,
            )["result"],
            f"{hkey}:00",
        )
        tiles = tiles_of(res)
        if not tiles:
            log(f"  {hkey}:00  empty")
            continue

        vals = values_by_tile(tiles, "average_temperature")
        row = {sid: vals.get(tid) for sid, tid in site_tile.items()}
        data["hourly"][hkey] = row
        sample = [v for v in row.values() if v is not None]
        log(f"  {hkey}:00  mean {sum(sample) / len(sample):.2f}C")
        save(data)

    # --- 3. forecast -------------------------------------------------------
    now = datetime.now(PHOENIX_TZ)
    log("\nforecast: walking forward until the API returns empty")
    data["forecast"] = {}          # always refresh; forecasts go stale
    for h in range(0, 13):
        when = now + timedelta(hours=h)
        stamp = when.strftime("%Y-%m-%dT%H:00")
        res = call_with_retry(
            lambda w=when: client.create_heatmap(
                polygon_aoi=aoi, granularity=GRAN,
                start_date=w.strftime("%Y-%m-%d"), start_time=w.strftime("%H:00"),
                filter_type=1, verbose=False,
            )["result"],
            f"+{h}h",
        )
        tiles = tiles_of(res)
        if not tiles:
            log(f"  +{h}h ({when:%H:00}) empty — horizon reached")
            data["notes"].append(
                f"Forecast reached +{h - 1}h from {now:%Y-%m-%d %H:%M} local; "
                f"the API returns an empty tile list beyond that rather than an error."
            )
            break

        vals = values_by_tile(tiles, "average_temperature")
        row = {sid: vals.get(tid) for sid, tid in site_tile.items()}
        data["forecast"][stamp] = row
        sample = [v for v in row.values() if v is not None]
        log(f"  +{h}h ({when:%H:00})  mean {sum(sample) / len(sample):.2f}C")
        save(data)

    save(data)
    log(f"\nwrote {OUT_PATH}")
    log(f"  exposure layers : {sorted(data['exposure'].keys())}")
    log(f"  hourly hours    : {len(data['hourly'])}/24")
    log(f"  forecast hours  : {len(data['forecast'])}")
    try:
        rem = client.fetch_api_key_usage()["credit_summary"]["cycle_remaining_credits"]
        log(f"  credits left    : {rem:,}")
    except Exception:
        pass

    missing = [h for h in range(24) if f"{h:02d}" not in data["hourly"]]
    if missing or len(data["exposure"]) < len(THRESHOLDS_C):
        log("\nSome pieces are missing. Just run the script again — "
            "it resumes and only retries what failed.")


if __name__ == "__main__":
    main()
