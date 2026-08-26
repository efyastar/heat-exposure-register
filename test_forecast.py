#!/usr/bin/env python3
"""
Probe the forecast window.

The handbook says Create Heatmap supports forecasts up to 12 hours ahead.
The whole operational side of the tool depends on that, so verify it rather
than assume it: request several horizons and see which succeed.

+15h is included deliberately as a negative control — it should be rejected.
If it succeeds, the documented limit isn't the real limit and we get more room.

Uses a small AOI so each call is quick and cheap.
"""

import os
import sys
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()
from fortyguard import FortyGuardClient  # noqa: E402

PHOENIX_TZ = ZoneInfo("America/Phoenix")
HORIZONS_H = [int(h) for h in os.environ.get("HORIZONS", "-2,3,6,11,15").split(",")]
GRANULARITY = int(os.environ.get("GRAN", "100"))

# ~1 km box in central Phoenix — small on purpose.
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


def probe(client, when, label):
    """One heatmap request at a specific local Phoenix datetime."""
    date_str = when.strftime("%Y-%m-%d")
    time_str = when.strftime("%H:00")
    try:
        res = client.create_heatmap(
            polygon_aoi=SMALL_AOI,
            granularity=GRANULARITY,
            start_date=date_str,
            start_time=time_str,
            filter_type=1,
            verbose=False,
        )["result"]
        stats = res["stats_data"]["temperature_stats"]
        n = len(res["map_data"]["features"])
        print(f"  {label:<22} {date_str} {time_str}  OK   "
              f"tiles={n:<5} mean={stats['mean']:.2f}C  "
              f"spread={stats['maximum'] - stats['minimum']:.3f}")
        return True
    except Exception as exc:
        msg = str(exc).replace("\n", " ")[:160]
        print(f"  {label:<22} {date_str} {time_str}  FAILED  "
              f"{type(exc).__name__}: {msg}")
        return False


def main():
    client = FortyGuardClient()
    now = datetime.now(PHOENIX_TZ)
    print(f"Phoenix local time now: {now:%Y-%m-%d %H:%M} "
          f"(your machine: {datetime.now():%H:%M})\n")
    print("probing forecast horizons:")

    results = {}
    for h in HORIZONS_H:
        when = now + timedelta(hours=h)
        label = f"{'now' if h == 0 else f'{h:+d}h'}"
        results[h] = probe(client, when, label)

    ok = [h for h, good in results.items() if good and h > 0]
    print()
    if not ok:
        print("No future horizon worked — forecasting is unavailable to this key.")
        print("The tool becomes historical-exposure only. Still viable, less operational.")
    else:
        print(f"Forecast works up to at least +{max(ok)}h ahead.")
        if results.get(15):
            print("NOTE: +15h succeeded — the 12h documented cap isn't enforced here.")
        else:
            print("+15h rejected, consistent with the documented 12-hour limit.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
