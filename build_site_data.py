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

# The danger threshold is chosen from the data rather than hardcoded, so this
# pipeline works outside Phoenix. See probe_peak() and pick_threshold().
#   - too close to the observed max -> exceedance interpolates and goes negative
#   - too far below it -> every tile saturates at the full window and nothing ranks
# Set AUTO_THRESHOLD=0 to force FALLBACK_THRESHOLDS_C instead.
AUTO_THRESHOLD = os.environ.get("AUTO_THRESHOLD", "1") == "1"
THRESHOLD_MARGIN_C = float(os.environ.get("THRESHOLD_MARGIN_C", "2.0"))
PEAK_HOUR = os.environ.get("PEAK_HOUR", "15:00")   # local; hottest part of the day
FALLBACK_THRESHOLDS_C = [38.0, 40.0]
CONTEXT_OFFSET_C = 2.0     # the secondary, lower threshold shown for context
MAX_THRESHOLD_TRIES = 3
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


def probe_peak(client, aoi, dates):
    """Hottest temperature actually observed, from a few sample afternoons.

    One cheap single-hour call per date. Cheaper and more reliable than guessing
    a threshold and discovering after a 31-day exceedance run that it saturated.
    """
    peaks = []
    for d in dates:
        res = call_with_retry(
            lambda dd=d: client.create_heatmap(
                polygon_aoi=aoi, granularity=GRAN, start_date=dd,
                start_time=PEAK_HOUR, filter_type=1, verbose=False,
            )["result"],
            f"peak {d}",
        )
        if not tiles_of(res):
            log(f"    {d} {PEAK_HOUR}: no tiles")
            continue
        st = (res.get("stats_data") or {}).get("temperature_stats") or {}
        if "maximum" in st:
            peaks.append(st["maximum"])
            log(f"    {d} {PEAK_HOUR}: max {st['maximum']:.2f}C  "
                f"mean {st['mean']:.2f}C  min {st['minimum']:.2f}C")
    return max(peaks) if peaks else None


def pick_threshold(peak):
    """A threshold a safe margin below the observed peak, rounded to 0.5 C."""
    return round((peak - THRESHOLD_MARGIN_C) * 2) / 2


def classify_layer(vals, window_hours):
    """Is this exceedance layer usable for ranking?"""
    lo, hi = min(vals), max(vals)
    if lo < 0:
        return "too_high"          # interpolating past the edge of the data
    if hi - lo < 0.5:
        if hi >= window_hours * 0.98:
            return "too_low"       # every tile saturated at the full window
        return "flat"              # no spread to rank on
    return "ok"


def exceedance_layer(client, aoi, thr):
    res = call_with_retry(
        lambda t=thr: client.create_heatmap(
            polygon_aoi=aoi, granularity=GRAN,
            start_date=WINDOW_START, end_date=WINDOW_END, filter_type=4,
            analytic_type="exceedance", threshold=t, direction="above",
            verbose=False, timeout=900,
        )["result"],
        f">{thr}C",
    )
    return tiles_of(res)


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
        "thresholds_c": None,
        "exposure": {}, "hourly": {}, "forecast": {},
        "site_tile": None, "notes": [],
    }
    data["generated_at"] = datetime.now(PHOENIX_TZ).isoformat()
    site_tile = data.get("site_tile")

    # --- 1. threshold selection -------------------------------------------
    days = (datetime.strptime(WINDOW_END, "%Y-%m-%d")
            - datetime.strptime(WINDOW_START, "%Y-%m-%d")).days + 1
    window_hours = days * 24
    data["window_days"] = days

    thresholds = data.get("thresholds_c")
    if not thresholds:
        if AUTO_THRESHOLD:
            log(f"\nchoosing a threshold from the data "
                f"(sampling {PEAK_HOUR} on three days)")
            sample_dates = [WINDOW_START, PROFILE_DAY, WINDOW_END]
            peak = probe_peak(client, aoi, sample_dates)
            if peak is None:
                log("  peak probe failed — falling back to fixed thresholds")
                thresholds = list(FALLBACK_THRESHOLDS_C)
            else:
                primary = pick_threshold(peak)
                log(f"  observed peak {peak:.2f}C -> starting threshold "
                    f"{primary:.1f}C (peak minus {THRESHOLD_MARGIN_C:.1f})")
                data["peak_observed_c"] = round(peak, 2)
                thresholds = [primary]
        else:
            thresholds = list(FALLBACK_THRESHOLDS_C)

    # --- 2. exposure, with the threshold validated against the result ------
    log(f"\nexposure: hours above threshold, {WINDOW_START} .. {WINDOW_END} "
        f"({days} days)")

    primary_thr = thresholds[0]
    tries = 0
    while tries < MAX_THRESHOLD_TRIES:
        key = str(primary_thr)
        if key in data["exposure"]:
            log(f"  >{primary_thr}C  already done, skipping")
            break

        log(f"  >{primary_thr}C ...")
        tiles = exceedance_layer(client, aoi, primary_thr)
        if not tiles:
            log(f"    >{primary_thr}C returned no tiles — skipping")
            break

        if site_tile is None:
            site_tile = assign_tiles(SITES, tiles)
            data["site_tile"] = site_tile
            log(f"    mapped {len(site_tile)} sites to tiles")

        vals = [t["properties"]["value"] for t in tiles
                if t["properties"].get("value") is not None]
        verdict = classify_layer(vals, window_hours)
        log(f"    {len(tiles)} tiles | {min(vals):.2f}-{max(vals):.2f} h "
            f"| spread {max(vals) - min(vals):.2f} | {verdict}")

        if verdict == "ok":
            by_tile = values_by_tile(tiles, "value")
            data["exposure"][key] = {sid: by_tile.get(tid)
                                     for sid, tid in site_tile.items()}
            data["threshold_c"] = primary_thr
            save(data)
            break

        tries += 1
        if tries >= MAX_THRESHOLD_TRIES:
            log(f"    still {verdict} after {tries} tries — keeping "
                f"{primary_thr}C anyway")
            by_tile = values_by_tile(tiles, "value")
            data["exposure"][key] = {sid: by_tile.get(tid)
                                     for sid, tid in site_tile.items()}
            data["threshold_c"] = primary_thr
            save(data)
            break

        if verdict == "too_high":
            primary_thr -= 1.0
            log(f"    negative hours — interpolating past the data. "
                f"Lowering to {primary_thr}C")
        else:
            primary_thr += 1.0
            log(f"    no spread to rank on ({verdict}). "
                f"Raising to {primary_thr}C")

    if site_tile is None:
        sys.exit("ERROR: no exposure layer succeeded. Re-run to retry, or shorten "
                 "the window with WINDOW_START/WINDOW_END.")

    # A lower threshold, purely for context in the interface.
    context_thr = primary_thr - CONTEXT_OFFSET_C
    ckey = str(context_thr)
    if ckey not in data["exposure"]:
        log(f"  >{context_thr}C (context) ...")
        tiles = exceedance_layer(client, aoi, context_thr)
        if tiles:
            by_tile = values_by_tile(tiles, "value")
            data["exposure"][ckey] = {sid: by_tile.get(tid)
                                      for sid, tid in site_tile.items()}
            vals = [v for v in data["exposure"][ckey].values() if v is not None]
            log(f"    sites {min(vals):.2f}-{max(vals):.2f} hours")
            save(data)

    data["thresholds_c"] = [primary_thr, context_thr]
    data["context_threshold_c"] = context_thr
    save(data)

    # --- 3. hourly profile -------------------------------------------------
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

    # --- 4. forecast -------------------------------------------------------
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
    log(f"  threshold       : {data.get('threshold_c')} C"
        + (f"  (observed peak {data['peak_observed_c']} C)"
           if data.get("peak_observed_c") else ""))
    log(f"  exposure layers : {sorted(data['exposure'].keys())}")
    log(f"  hourly hours    : {len(data['hourly'])}/24")
    log(f"  forecast hours  : {len(data['forecast'])}")
    try:
        rem = client.fetch_api_key_usage()["credit_summary"]["cycle_remaining_credits"]
        log(f"  credits left    : {rem:,}")
    except Exception:
        pass

    missing = [h for h in range(24) if f"{h:02d}" not in data["hourly"]]
    if missing or len(data["exposure"]) < 2:
        log("\nSome pieces are missing. Just run the script again — "
            "it resumes and only retries what failed.")


if __name__ == "__main__":
    main()
