#!/usr/bin/env python3
"""
Build the model feature table.

  Target   : hours above 40 C per tile  (FortyGuard exceedance layer)
  Features : urban form per tile        (OpenStreetMap)

Reads  phoenix_exc40.geojson   (saved from the notebook)
Writes tile_features.csv       (one row per tile, ready to model)

Quick test on a subset before committing to the full run:
    SAMPLE=400 python3 build_features.py

Full run:
    python3 build_features.py

OSM downloads are cached in osm_cache/ — the first run is the slow one.
"""

import json
import os
import sys
import time
import warnings

import geopandas as gpd
import pandas as pd
from shapely.geometry import shape

warnings.filterwarnings("ignore")

TILES_PATH = os.environ.get("TILES", "phoenix_exc40.geojson")
OUT_PATH = os.environ.get("OUT", "tile_features.csv")
SAMPLE = int(os.environ.get("SAMPLE", "0"))
CACHE_DIR = "osm_cache"

# Projected CRS so areas are in m^2 and distances in metres.
# UTM zone 12N covers Arizona. Change if you move cities.
PROJ_CRS = "EPSG:32612"

# What we pull from OpenStreetMap. Each entry becomes one or more features.
OSM_LAYERS = {
    "building": {"building": True},
    "green": {
        "leisure": ["park", "garden", "pitch", "golf_course"],
        "landuse": ["grass", "forest", "recreation_ground", "meadow", "village_green"],
        "natural": ["wood", "scrub", "grassland"],
    },
    "water": {"natural": ["water"], "waterway": True, "landuse": ["reservoir"]},
    "road": {"highway": True},
    "tree": {"natural": ["tree"]},
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- inputs -----------------------------------------------------------------

def load_tiles(path):
    if not os.path.exists(path):
        sys.exit(f"ERROR: {path} not found. Run the notebook cell that saves it first.")

    with open(path) as fh:
        gj = json.load(fh)

    rows, geoms = [], []
    for f in gj.get("features", []):
        props = f.get("properties", {})
        geom = f.get("geometry")
        if not geom:
            continue
        rows.append({
            "tile_id": props.get("tile_id"),
            "hours_above_40": props.get("value"),
        })
        geoms.append(shape(geom))

    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf = gdf.dropna(subset=["hours_above_40"])
    log(f"loaded {len(gdf)} tiles from {path}")
    return gdf.reset_index(drop=True)


def fetch_osm(aoi_wgs84, tags, name):
    """Download one OSM layer for the AOI, cached to disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{name}.gpkg")

    if os.path.exists(path):
        gdf = gpd.read_file(path)
        log(f"  {name}: {len(gdf)} features (cached)")
        return gdf

    try:
        import osmnx as ox
    except ImportError:
        sys.exit("ERROR: osmnx not installed. Run:  pip install osmnx")

    log(f"  {name}: downloading from OpenStreetMap...")
    try:
        # osmnx >= 2 renamed this; fall back for 1.x
        getter = getattr(ox, "features_from_polygon", None) or ox.geometries_from_polygon
        gdf = getter(aoi_wgs84, tags)
    except Exception as exc:
        log(f"  {name}: download FAILED ({type(exc).__name__}: {exc}) — treating as empty")
        gdf = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")

    if len(gdf):
        # OSM results carry list-valued columns that no file format will store.
        gdf = gdf.reset_index()[["geometry"]]
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]

    if len(gdf):
        gdf.to_file(path, driver="GPKG")
    log(f"  {name}: {len(gdf)} features")
    return gdf


# --- feature computations ---------------------------------------------------

def area_fraction(tiles, polys, col):
    """Share of each tile's area covered by these polygons (0-1)."""
    tiles[col] = 0.0
    if polys is None or polys.empty:
        return tiles

    polys = polys[polys.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    if polys.empty:
        return tiles

    inter = gpd.overlay(
        tiles[["tile_id", "geometry"]], polys[["geometry"]], how="intersection"
    )
    if inter.empty:
        return tiles

    covered = inter.groupby("tile_id")["geometry"].apply(lambda g: g.area.sum())
    tiles[col] = (
        tiles["tile_id"].map(covered).fillna(0.0) / tiles.geometry.area
    ).clip(0, 1)
    return tiles


def line_density(tiles, lines, col):
    """Metres of line per tile, normalised to metres per m^2 of tile."""
    tiles[col] = 0.0
    if lines is None or lines.empty:
        return tiles

    lines = lines[lines.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    if lines.empty:
        return tiles

    inter = gpd.overlay(
        tiles[["tile_id", "geometry"]], lines[["geometry"]],
        how="intersection", keep_geom_type=False,
    )
    if inter.empty:
        return tiles

    inter = inter[inter.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    if inter.empty:
        return tiles

    length = inter.groupby("tile_id")["geometry"].apply(lambda g: g.length.sum())
    tiles[col] = tiles["tile_id"].map(length).fillna(0.0) / tiles.geometry.area
    return tiles


def point_count(tiles, pts, col):
    """How many point features fall inside each tile."""
    tiles[col] = 0
    if pts is None or pts.empty:
        return tiles

    pts = pts[pts.geometry.geom_type == "Point"]
    if pts.empty:
        return tiles

    joined = gpd.sjoin(
        pts[["geometry"]], tiles[["tile_id", "geometry"]], predicate="within"
    )
    counts = joined.groupby("tile_id").size()
    tiles[col] = tiles["tile_id"].map(counts).fillna(0).astype(int)
    return tiles


def distance_to_nearest(tiles, target, col, cap=5000.0):
    """Distance in metres from each tile centre to the nearest such feature."""
    tiles[col] = cap
    if target is None or target.empty:
        return tiles

    centroids = gpd.GeoDataFrame(
        tiles[["tile_id"]].copy(), geometry=tiles.geometry.centroid, crs=tiles.crs
    )
    near = gpd.sjoin_nearest(
        centroids, target[["geometry"]], how="left", distance_col="_d"
    )
    dist = near.groupby("tile_id")["_d"].min()
    tiles[col] = tiles["tile_id"].map(dist).fillna(cap).clip(0, cap)
    return tiles


# --- main -------------------------------------------------------------------

def main():
    tiles = load_tiles(TILES_PATH)

    if SAMPLE and SAMPLE < len(tiles):
        tiles = tiles.sample(SAMPLE, random_state=0).reset_index(drop=True)
        log(f"SAMPLE mode: using {len(tiles)} tiles")

    aoi_wgs84 = tiles.geometry.union_all().convex_hull
    tiles_p = tiles.to_crs(PROJ_CRS)

    log("fetching OpenStreetMap layers")
    osm = {
        name: fetch_osm(aoi_wgs84, tags, name).to_crs(PROJ_CRS)
        for name, tags in OSM_LAYERS.items()
    }

    log("computing features")
    tiles_p = area_fraction(tiles_p, osm["building"], "building_frac")
    log("  building_frac done")
    tiles_p = area_fraction(tiles_p, osm["green"], "green_frac")
    log("  green_frac done")
    tiles_p = area_fraction(tiles_p, osm["water"], "water_frac")
    log("  water_frac done")
    tiles_p = line_density(tiles_p, osm["road"], "road_density")
    log("  road_density done")
    tiles_p = point_count(tiles_p, osm["tree"], "tree_count")
    log("  tree_count done")
    tiles_p = distance_to_nearest(tiles_p, osm["green"], "dist_green_m")
    log("  dist_green_m done")
    tiles_p = distance_to_nearest(tiles_p, osm["water"], "dist_water_m")
    log("  dist_water_m done")

    # Coordinates: let the model account for smooth regional gradients
    # (elevation, distance from the desert edge) that urban form doesn't explain.
    centroids_wgs = tiles_p.geometry.centroid.to_crs("EPSG:4326")
    tiles_p["lon"] = centroids_wgs.x
    tiles_p["lat"] = centroids_wgs.y
    tiles_p["tile_area_m2"] = tiles_p.geometry.area

    cols = [
        "tile_id", "hours_above_40", "lon", "lat", "tile_area_m2",
        "building_frac", "green_frac", "water_frac", "road_density",
        "tree_count", "dist_green_m", "dist_water_m",
    ]
    out = pd.DataFrame(tiles_p[cols])
    out.to_csv(OUT_PATH, index=False)

    log(f"wrote {OUT_PATH}  ({len(out)} rows, {len(cols) - 1} columns)")
    print()
    print(out[[c for c in cols if c != "tile_id"]].describe().T[
        ["mean", "std", "min", "max"]
    ].round(4))


if __name__ == "__main__":
    main()
