#!/usr/bin/env python3
"""
Compact everything the web app needs into one small JSON file.

site_data.json + grid_exc40.geojson (2.1 MB of polygons) -> web/data.json (~200 KB)

The grid becomes flat coordinate/value arrays instead of GeoJSON polygons: the
tiles are a regular lattice, so a centroid and a tile size carry the same
information at a tenth of the size.

    python3 prepare_web_data.py
"""

import json
import os
from statistics import mean

SITE_PATH = os.environ.get("SITES", "site_data.json")
GRID_PATH = os.environ.get("GRID", "grid_exc40.geojson")
OUT_DIR = os.environ.get("OUT_DIR", "web")
OUT_PATH = os.path.join(OUT_DIR, "data.json")

# No longer hardcoded — build_site_data.py picks it from the observed data.
FALLBACK_THRESHOLD_C = 40.0


def centroid(geom):
    ring = geom["coordinates"][0]
    xs = [p[0] for p in ring[:-1]]
    ys = [p[1] for p in ring[:-1]]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def tile_size(geom):
    ring = geom["coordinates"][0]
    xs = [p[0] for p in ring[:-1]]
    ys = [p[1] for p in ring[:-1]]
    return max(xs) - min(xs), max(ys) - min(ys)


def danger_window(hourly, threshold):
    """First and last hour at or above the threshold, and the count."""
    hot = [h for h, t in enumerate(hourly) if t is not None and t >= threshold]
    if not hot:
        return {"start": None, "end": None, "hours": 0}
    return {"start": min(hot), "end": max(hot), "hours": len(hot)}


def main():
    with open(SITE_PATH) as fh:
        sd = json.load(fh)
    with open(GRID_PATH) as fh:
        grid = json.load(fh)

    os.makedirs(OUT_DIR, exist_ok=True)

    # --- thresholds --------------------------------------------------------
    threshold = sd.get("threshold_c") or FALLBACK_THRESHOLD_C
    context = sd.get("context_threshold_c")
    exposure = sd.get("exposure", {})

    primary_map = exposure.get(str(threshold), {})
    if not primary_map:                       # tolerate 40 vs 40.0 key formatting
        for k, v in exposure.items():
            if abs(float(k) - threshold) < 1e-6:
                primary_map = v
                break
    context_map = exposure.get(str(context), {}) if context is not None else {}

    print(f"threshold: {threshold} C"
          + (f"  (observed peak {sd['peak_observed_c']} C)"
             if sd.get("peak_observed_c") else "  (fixed)"))

    # --- sites -------------------------------------------------------------
    hourly_raw = sd.get("hourly", {})

    sites = []
    for s in sd["sites"]:
        hourly = [hourly_raw.get(f"{h:02d}", {}).get(s["id"]) for h in range(24)]
        pv, cv = primary_map.get(s["id"]), context_map.get(s["id"])
        sites.append({
            "id": s["id"],
            "name": s["name"],
            "lat": s["lat"],
            "lon": s["lon"],
            "exc": round(pv, 2) if pv is not None else None,
            "exc_ctx": round(cv, 2) if cv is not None else None,
            "hourly": [round(t, 2) if t is not None else None for t in hourly],
            "window": danger_window(hourly, threshold),
        })

    ranked = sorted(
        [s for s in sites if s["exc"] is not None],
        key=lambda s: s["exc"], reverse=True,
    )
    for i, s in enumerate(ranked):
        s["rank"] = i + 1

    # --- forecast ----------------------------------------------------------
    fc = sd.get("forecast", {})
    times = sorted(fc.keys())
    forecast = {
        "times": times,
        "per_site": {
            s["id"]: [
                round(fc[t].get(s["id"]), 2) if fc[t].get(s["id"]) is not None else None
                for t in times
            ]
            for s in sites
        },
    }
    if times:
        forecast["mean"] = [
            round(mean([v for v in fc[t].values() if v is not None]), 2)
            for t in times
        ]

    # --- grid --------------------------------------------------------------
    feats = grid["features"]
    dlon, dlat = tile_size(feats[0]["geometry"])
    lons, lats, vals = [], [], []
    for f in feats:
        v = f["properties"].get("value")
        if v is None:
            continue
        x, y = centroid(f["geometry"])
        lons.append(round(x, 5))
        lats.append(round(y, 5))
        vals.append(round(v, 1))

    grid_out = {
        "lon": lons, "lat": lats, "val": vals,
        "dlon": round(dlon, 6), "dlat": round(dlat, 6),
        "min": min(vals), "max": max(vals),
        "mean": round(mean(vals), 1),
        "n": len(vals),
    }

    # --- assemble ----------------------------------------------------------
    win = sd.get("window", {})
    out = {
        "generated_at": sd.get("generated_at"),
        "window": win,
        "window_days": sd.get("window_days", 31),
        "profile_day": sd.get("profile_day"),
        "threshold_c": threshold,
        "context_threshold_c": context,
        "peak_observed_c": sd.get("peak_observed_c"),
        "sites": ranked + [s for s in sites if s.get("rank") is None],
        "forecast": forecast,
        "grid": grid_out,
        "notes": sd.get("notes", []),
        "costs": {
            "ed_visit_usd": 757,
            "hospital_admission_usd": 14900,
            "source": "HCUP 2020, via Center for American Progress",
        },
    }

    with open(OUT_PATH, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))

    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"wrote {OUT_PATH} ({size_kb:.0f} KB)")
    print(f"  sites    : {len(out['sites'])}")
    print(f"  grid     : {grid_out['n']} tiles, "
          f"{grid_out['min']}-{grid_out['max']} h (mean {grid_out['mean']})")
    print(f"  forecast : {len(times)} hours")
    print(f"  hourly   : {sum(1 for h in ranked[0]['hourly'] if h is not None)}/24 "
          f"for {ranked[0]['name']}")
    print()
    print(f"  rank  site                                  hours>{threshold}C   danger window")
    for s in out["sites"]:
        w = s["window"]
        span = (f"{w['start']:02d}:00-{w['end']:02d}:00 ({w['hours']}h)"
                if w["start"] is not None else "none")
        per_day = s["exc"] / out["window_days"]
        print(f"  {s.get('rank', '-'):>4}  {s['name']:<36}  {s['exc']:>8.1f}"
              f"  ({per_day:.2f}/day)   {span}")
    lo = min(s["exc"] for s in out["sites"] if s["exc"] is not None)
    hi = max(s["exc"] for s in out["sites"] if s["exc"] is not None)
    gr = grid_out["max"] - grid_out["min"]
    print(f"\n  register spans {hi - lo:.1f} h "
          f"({(hi - lo) / out['window_days']:.2f} h/day) — "
          f"{(hi - lo) / gr * 100:.0f}% of the full grid range")


if __name__ == "__main__":
    main()
