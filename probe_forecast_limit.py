#!/usr/bin/env python3
"""
Find the real forecast boundary, and show what a beyond-the-edge response
actually looks like.

The previous probe reported failures that were really my parser missing a key.
This one makes no assumptions: it prints the response structure at each horizon
and only calls something a failure if the request itself raises.
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()
from fortyguard import FortyGuardClient  # noqa: E402

PHOENIX_TZ = ZoneInfo("America/Phoenix")
HORIZONS_H = [int(h) for h in os.environ.get("HORIZONS", "7,9,11,13").split(",")]

SMALL_AOI = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [-112.078, 33.448], [-112.068, 33.448],
            [-112.068, 33.456], [-112.078, 33.456],
            [-112.078, 33.448],
        ]]},
    }],
}


def main():
    client = FortyGuardClient()
    now = datetime.now(PHOENIX_TZ)
    print(f"Phoenix now: {now:%Y-%m-%d %H:%M}\n")

    for h in HORIZONS_H:
        when = now + timedelta(hours=h)
        print(f"=== {h:+d}h  ->  {when:%Y-%m-%d %H:00} local ===")
        try:
            resp = client.create_heatmap(
                polygon_aoi=SMALL_AOI, granularity=100,
                start_date=when.strftime("%Y-%m-%d"),
                start_time=when.strftime("%H:00"),
                filter_type=1, verbose=False,
            )
        except Exception as exc:
            print(f"  REQUEST RAISED: {type(exc).__name__}: {str(exc)[:220]}\n")
            continue

        res = resp.get("result")
        if res is None:
            print(f"  no 'result' key. top-level keys: {list(resp.keys())}\n")
            continue

        print(f"  result keys : {list(res.keys())}")

        stats = res.get("stats_data")
        if isinstance(stats, dict):
            print(f"  stats keys  : {list(stats.keys())}")
            temp = stats.get("temperature_stats")
            if temp:
                print(f"  temps       : min={temp['minimum']:.3f} "
                      f"max={temp['maximum']:.3f} "
                      f"spread={temp['maximum'] - temp['minimum']:.4f}")
            else:
                print("  temps       : temperature_stats ABSENT")
        else:
            print(f"  stats_data  : {type(stats).__name__} -> {str(stats)[:160]}")

        feats = (res.get("map_data") or {}).get("features") or []
        print(f"  tiles       : {len(feats)}")
        if feats:
            print(f"  first props : {feats[0].get('properties')}")
        print()


if __name__ == "__main__":
    main()
