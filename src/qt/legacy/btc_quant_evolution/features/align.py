# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import pandas as pd


def align_asof_features(
    candles: pd.DataFrame,
    features: pd.DataFrame,
    tolerance: pd.Timedelta | None,
) -> pd.DataFrame:
    """Attach only feature rows available no later than each candle close."""
    if "date" not in candles.columns:
        raise ValueError("candles must include date")
    if "available_at" not in features.columns:
        raise ValueError("features must include available_at")

    candle_frame = candles.copy()
    candle_frame["date"] = pd.to_datetime(candle_frame["date"], utc=True, errors="raise").dt.as_unit("ns")
    feature_frame = features.copy()
    feature_frame["available_at"] = pd.to_datetime(
        feature_frame["available_at"],
        utc=True,
        errors="raise",
    ).dt.as_unit("ns")
    return pd.merge_asof(
        candle_frame.sort_values("date"),
        feature_frame.sort_values("available_at").drop(columns=["timestamp"], errors="ignore"),
        left_on="date",
        right_on="available_at",
        direction="backward",
        tolerance=tolerance,
    )

