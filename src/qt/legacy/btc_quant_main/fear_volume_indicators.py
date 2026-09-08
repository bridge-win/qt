# Migrated verbatim from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import pandas as pd


def add_fear_volume_features(
    dataframe: pd.DataFrame,
    *,
    fear_window: int,
    volume_window: int,
) -> pd.DataFrame:
    result = dataframe.copy()
    prior_high = result["high"].rolling(fear_window).max().shift(1)
    prior_volume = result["volume"].rolling(volume_window).mean().shift(1)

    result["fear_drawdown"] = ((prior_high - result["close"]) / prior_high).clip(lower=0)
    result["volume_ratio"] = result["volume"] / prior_volume
    result["atr_pct"] = result["atr"] / result["close"]
    return result


def fear_volume_entry_signal(
    dataframe: pd.DataFrame,
    *,
    min_drawdown: float,
    min_volume_ratio: float,
    min_atr_pct: float,
) -> pd.Series:
    return (
        (dataframe["volume"] > 0)
        & (dataframe["fear_drawdown"] >= min_drawdown)
        & (dataframe["volume_ratio"] >= min_volume_ratio)
        & (dataframe["atr_pct"] >= min_atr_pct)
        & (dataframe["close"] > dataframe["open"])
    )

