# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import pandas as pd


def derive_orderflow_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Derive spread and depth imbalance from locally materialized snapshots."""
    result = frame.copy()
    if {"best_bid", "best_ask"} <= set(result.columns):
        midpoint = (result["best_bid"] + result["best_ask"]) / 2
        result["spread_bps"] = (result["best_ask"] - result["best_bid"]) / midpoint * 10_000
    if {"bid_depth", "ask_depth"} <= set(result.columns):
        total_depth = result["bid_depth"] + result["ask_depth"]
        result["depth_imbalance"] = (result["bid_depth"] - result["ask_depth"]) / total_depth.where(total_depth != 0)
    return result

