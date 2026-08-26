#!/usr/bin/env python3
"""
Add a 5 a.m. temperature column to the feature table.

Exceedance hours are dominated by daytime heat, when a city is thermally
uniform. The urban heat island is a night-time effect: built surfaces release
stored heat after dark while vegetated ones cool. If urban form predicts
anything, pre-dawn temperature is where it should show.

Pulls the same AOI at the same granularity so tile_ids line up with
tile_features.csv, then writes tile_features_night.csv.

    python3 add_night_target.py
    TARGET=night_temp_c python3 train_model.py
"""

import os
import sys

import pandas as pd
from dotenv import load_dotenv

load_dotenv()
from fortyguard import FortyGuardClient  # noqa: E402

FEATURES_PATH = os.environ.get("IN", "tile_features.csv")
OUT_PATH = os.environ.get("OUT", "tile_features_night.csv")
START_DATE = os.environ.get("DATE", "2026-08-03")
START_TIME = os.environ.get("TIME", "05:00")   # local Phoenix time
GRANULARITY = int(os.environ.get("GRAN", "100"))

# Must match the AOI used for tile_features.csv, or tile_ids won't align.
WIDE = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [-112.10, 33.36], [-112.04, 33.36],
            [-112.04, 33.48], [-112.10, 33.48],
            [-112.10, 33.36],
        ]]},
    }],
}


def main():
    if not os.path.exists(FEATURES_PATH):
        sys.exit(f"ERROR: {FEATURES_PATH} not found. Run build_features.py first.")

    feats = pd.read_csv(FEATURES_PATH)
    print(f"feature table: {len(feats)} tiles")

    client = FortyGuardClient()
    print(f"requesting {START_TIME} local on {START_DATE} at {GRANULARITY}m...")
    res = client.create_heatmap(
        polygon_aoi=WIDE,
        granularity=GRANULARITY,
        start_date=START_DATE,
        start_time=START_TIME,
        filter_type=1,
    )["result"]

    tiles = res["map_data"]["features"]
    night = pd.DataFrame([
        {"tile_id": f["properties"]["tile_id"],
         "night_temp_c": f["properties"]["average_temperature"]}
        for f in tiles
    ])
    print(f"night layer: {len(night)} tiles  "
          f"range {night.night_temp_c.min():.3f}-{night.night_temp_c.max():.3f} C  "
          f"spread {night.night_temp_c.max() - night.night_temp_c.min():.3f}")

    if len(night) != len(feats):
        print(f"WARNING: tile counts differ ({len(night)} vs {len(feats)}). "
              f"Joining on tile_id; unmatched rows will be dropped.")

    merged = feats.merge(night, on="tile_id", how="inner")
    print(f"merged: {len(merged)} tiles")

    if merged.empty:
        sys.exit("ERROR: no tiles matched. The AOI or granularity must differ "
                 "from the one used for tile_features.csv.")

    merged.to_csv(OUT_PATH, index=False)
    print(f"wrote {OUT_PATH}")
    print(f"\nnow run:  TARGET=night_temp_c IN={OUT_PATH} python3 train_model.py")


if __name__ == "__main__":
    main()
