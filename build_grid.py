#!/usr/bin/env python3
"""
Pull the full tile grid (geometry included) for the site AOI.

build_site_data.py kept only the twelve demo sites and threw the other ~6,000
tiles away. Those tiles are what make the app a map instead of a table, and
what let a user drop in their own coordinates and get a real answer.

One API call. Saves grid_exc40.geojson.

    python3 build_grid.py
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()
from fortyguard import FortyGuardClient  # noqa: E402

# Reuse the exact AOI and window the site data used, so everything lines up.
from build_site_data import (  # noqa: E402
    SITES, build_aoi, tiles_of, call_with_retry,
    WINDOW_START, WINDOW_END, GRAN,
)

OUT_PATH = os.environ.get("OUT", "grid_exceedance.geojson")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "180"))
SITE_DATA = os.environ.get("SITES", "site_data.json")


def resolve_threshold():
    """Use whatever threshold build_site_data.py settled on.

    Hardcoding this is how the map and the register end up measuring different
    things: the register would rank sites at one threshold while the map shaded
    them at another, and the two would disagree on screen.
    """
    override = os.environ.get("THRESHOLD")
    if override:
        return float(override)
    try:
        with open(SITE_DATA) as fh:
            thr = json.load(fh).get("threshold_c")
        if thr is not None:
            print(f"threshold {thr}C (from {SITE_DATA})")
            return float(thr)
    except Exception as exc:
        print(f"could not read {SITE_DATA} ({type(exc).__name__})")
    print("falling back to 40.0C")
    return 40.0


THRESHOLD = resolve_threshold()


def main():
    client = FortyGuardClient(timeout=HTTP_TIMEOUT)
    aoi = build_aoi(SITES)

    print(f"exceedance > {THRESHOLD}C, {WINDOW_START} .. {WINDOW_END}, full grid")
    res = call_with_retry(
        lambda: client.create_heatmap(
            polygon_aoi=aoi, granularity=GRAN,
            start_date=WINDOW_START, end_date=WINDOW_END, filter_type=4,
            analytic_type="exceedance", threshold=THRESHOLD, direction="above",
            verbose=False, timeout=900,
        )["result"],
        "grid",
    )

    tiles = tiles_of(res)
    if not tiles:
        raise SystemExit("ERROR: no tiles returned. Re-run to retry.")

    vals = [t["properties"].get("value") for t in tiles]
    vals = [v for v in vals if v is not None]
    print(f"tiles: {len(tiles)}")
    print(f"hours above {THRESHOLD}C: min={min(vals):.2f} max={max(vals):.2f} "
          f"mean={sum(vals) / len(vals):.2f}")

    payload = {
        "type": "FeatureCollection",
        "threshold_c": THRESHOLD,
        "window": {"start": WINDOW_START, "end": WINDOW_END},
        "features": tiles,
    }
    with open(OUT_PATH, "w") as fh:
        json.dump(payload, fh)

    size_mb = os.path.getsize(OUT_PATH) / 1e6
    print(f"wrote {OUT_PATH} ({size_mb:.1f} MB)")
    if size_mb > 40:
        print("NOTE: large for a git repo — we can simplify geometry if needed.")


if __name__ == "__main__":
    main()
