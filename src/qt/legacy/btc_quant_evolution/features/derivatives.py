# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import pandas as pd


def derive_derivatives_features(frame: pd.DataFrame, *, zscore_window: int = 30) -> pd.DataFrame:
    """Derive deterministic funding, positioning, and taker-flow features."""
    if zscore_window < 2:
        raise ValueError("zscore_window must be at least 2")

    result = frame.copy()
    if "funding_rate" in result:
        rolling = result["funding_rate"].rolling(zscore_window, min_periods=zscore_window)
        result["funding_rate_zscore"] = (result["funding_rate"] - rolling.mean()) / rolling.std(ddof=0).replace(0, pd.NA)
    if "open_interest" in result:
        result["open_interest_change"] = result["open_interest"].pct_change()
    if {"taker_buy_volume", "taker_sell_volume"} <= set(result.columns):
        total = result["taker_buy_volume"] + result["taker_sell_volume"]
        result["taker_buy_sell_imbalance"] = (
            (result["taker_buy_volume"] - result["taker_sell_volume"]) / total.where(total != 0)
        )
    return result

