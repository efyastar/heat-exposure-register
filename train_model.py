#!/usr/bin/env python3
"""
Train the heat model and evaluate it honestly.

Predicts hours above 40 C per tile from urban form.

The evaluation is deliberately strict. Neighbouring tiles are highly
correlated, so a random train/test split would let the model memorise a
neighbourhood and score brilliantly on tiles metres away — a number that means
nothing. Instead the city is cut into spatial blocks and whole blocks are held
out, so the model is always predicting somewhere it has never seen.

Three models are compared, and the comparison IS the finding:

  mean       — predict the average everywhere (the floor)
  coords     — latitude/longitude only, no urban form at all
  coords+form— adds buildings, green space, roads, water

If coords+form does not beat coords, urban form adds nothing beyond a smooth
geographic gradient, and the simulator has no basis. That result would be worth
reporting honestly rather than hiding.

Usage:  python3 train_model.py
"""

import json
import os
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import GroupKFold

IN_PATH = os.environ.get("IN", "tile_features.csv")
MODEL_PATH = os.environ.get("MODEL", "heat_model.joblib")
METRICS_PATH = os.environ.get("METRICS", "model_metrics.json")
TARGET = os.environ.get("TARGET", "hours_above_40")
N_BLOCKS = int(os.environ.get("N_BLOCKS", "6"))   # grid is N_BLOCKS x N_BLOCKS

COORD_FEATURES = ["lon", "lat"]
FORM_FEATURES = [
    "building_frac", "green_frac", "water_frac",
    "road_density", "tree_count", "dist_green_m", "dist_water_m",
]


def spatial_blocks(df, n=N_BLOCKS):
    """Label each tile with a block id, so whole regions are held out together."""
    lon_edges = np.quantile(df["lon"], np.linspace(0, 1, n + 1))
    lat_edges = np.quantile(df["lat"], np.linspace(0, 1, n + 1))
    ix = np.clip(np.digitize(df["lon"], lon_edges[1:-1]), 0, n - 1)
    iy = np.clip(np.digitize(df["lat"], lat_edges[1:-1]), 0, n - 1)
    return ix * n + iy


def cross_validate(X, y, groups, label, n_splits=5):
    """Block-held-out CV. Returns out-of-fold predictions and scores."""
    oof = np.full(len(y), np.nan)
    gkf = GroupKFold(n_splits=min(n_splits, len(np.unique(groups))))

    for train_idx, test_idx in gkf.split(X, y, groups):
        model = HistGradientBoostingRegressor(
            max_iter=400, learning_rate=0.06,
            max_depth=6, min_samples_leaf=25, random_state=0,
        )
        model.fit(X.iloc[train_idx], y.iloc[train_idx])
        oof[test_idx] = model.predict(X.iloc[test_idx])

    r2 = r2_score(y, oof)
    mae = mean_absolute_error(y, oof)
    print(f"  {label:<14} R2={r2:6.3f}   MAE={mae:.3f} hours")
    return oof, r2, mae


def main():
    if not os.path.exists(IN_PATH):
        sys.exit(f"ERROR: {IN_PATH} not found. Run build_features.py first.")

    df = pd.read_csv(IN_PATH).dropna(subset=[TARGET])
    available = [c for c in FORM_FEATURES if c in df.columns]
    missing = set(FORM_FEATURES) - set(available)
    if missing:
        print(f"note: missing feature columns {sorted(missing)}")

    print(f"tiles: {len(df)}")
    print(f"target: {TARGET}  "
          f"mean={df[TARGET].mean():.2f}  sd={df[TARGET].std():.3f}  "
          f"range={df[TARGET].min():.2f}-{df[TARGET].max():.2f}")

    groups = spatial_blocks(df)
    print(f"spatial blocks: {len(np.unique(groups))} "
          f"(whole blocks held out, never single tiles)\n")

    y = df[TARGET]

    print("block-held-out cross-validation:")
    baseline_mae = mean_absolute_error(y, np.full(len(y), y.mean()))
    print(f"  {'mean':<14} R2= 0.000   MAE={baseline_mae:.3f} hours")

    _, r2_coords, mae_coords = cross_validate(
        df[COORD_FEATURES], y, groups, "coords")
    oof_full, r2_full, mae_full = cross_validate(
        df[COORD_FEATURES + available], y, groups, "coords+form")

    lift = r2_full - r2_coords
    print(f"\n  urban form adds {lift:+.3f} R2 over geography alone")
    if lift < 0.02:
        print("  -> WEAK. Urban form barely helps; the simulator's premise is shaky.")
    elif lift < 0.10:
        print("  -> MODEST but real. Report the effect size honestly.")
    else:
        print("  -> STRONG. Urban form genuinely predicts heat here.")

    # Final model on everything, for the app to use.
    final = HistGradientBoostingRegressor(
        max_iter=400, learning_rate=0.06,
        max_depth=6, min_samples_leaf=25, random_state=0,
    )
    features = COORD_FEATURES + available
    final.fit(df[features], y)

    print("\npermutation importance (drop in R2 when a feature is shuffled):")
    perm = permutation_importance(
        final, df[features], y, n_repeats=8, random_state=0, scoring="r2"
    )
    order = np.argsort(perm.importances_mean)[::-1]
    for i in order:
        print(f"  {features[i]:<16} {perm.importances_mean[i]:+.4f} "
              f"(+/- {perm.importances_std[i]:.4f})")

    try:
        import joblib
        joblib.dump({"model": final, "features": features}, MODEL_PATH)
        print(f"\nsaved {MODEL_PATH}")
    except ImportError:
        print("\nnote: joblib not installed, model not saved "
              "(pip install joblib)")

    metrics = {
        "n_tiles": int(len(df)),
        "target": TARGET,
        "target_mean": float(y.mean()),
        "target_sd": float(y.std()),
        "n_blocks": int(len(np.unique(groups))),
        "mae_baseline_mean": float(baseline_mae),
        "r2_coords": float(r2_coords), "mae_coords": float(mae_coords),
        "r2_coords_form": float(r2_full), "mae_coords_form": float(mae_full),
        "form_lift_r2": float(lift),
        "permutation_importance": {
            features[i]: float(perm.importances_mean[i]) for i in order
        },
    }
    with open(METRICS_PATH, "w") as fh:
        json.dump(metrics, fh, indent=2)
    print(f"saved {METRICS_PATH}")

    df["predicted"] = oof_full
    df["residual"] = y - oof_full
    df.to_csv("tile_predictions.csv", index=False)
    print("saved tile_predictions.csv (out-of-fold predictions for mapping)")


if __name__ == "__main__":
    main()
