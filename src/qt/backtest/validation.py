"""Backtest input validation and dataset fingerprints."""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype

REQUIRED_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def validate_ohlcv(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Validate and normalize point-in-time OHLCV input for reproducible backtests."""

    if ohlcv.empty:
        raise ValueError("ohlcv is empty")
    missing = [column for column in REQUIRED_OHLCV_COLUMNS if column not in ohlcv.columns]
    if missing:
        raise ValueError(f"ohlcv is missing required columns: {', '.join(missing)}")
    if not isinstance(ohlcv.index, pd.DatetimeIndex):
        raise ValueError("ohlcv index must be a DatetimeIndex")
    if ohlcv.index.tz is None:
        raise ValueError("ohlcv index must be timezone-aware UTC")
    if str(ohlcv.index.tz) != "UTC":
        ohlcv = ohlcv.copy()
        ohlcv.index = ohlcv.index.tz_convert("UTC")
    if not ohlcv.index.is_monotonic_increasing:
        raise ValueError("ohlcv index must be sorted ascending")
    if not ohlcv.index.is_unique:
        raise ValueError("ohlcv index must not contain duplicate timestamps")

    numeric = ohlcv.loc[:, REQUIRED_OHLCV_COLUMNS].apply(pd.to_numeric, errors="coerce")
    values = numeric.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("ohlcv values must be finite numbers")
    if (numeric.loc[:, ("open", "high", "low", "close")] <= 0).any().any():
        raise ValueError("ohlcv open/high/low/close values must be positive")
    if (numeric["volume"] < 0).any():
        raise ValueError("ohlcv volume must be non-negative")
    if (numeric["high"] < numeric[["open", "close"]].max(axis=1)).any():
        raise ValueError("ohlcv high must be at least max(open, close)")
    if (numeric["low"] > numeric[["open", "close"]].min(axis=1)).any():
        raise ValueError("ohlcv low must be at most min(open, close)")

    normalized = ohlcv.copy()
    for column in REQUIRED_OHLCV_COLUMNS:
        normalized[column] = numeric[column].astype(float)
    return normalized


def ohlcv_fingerprint(ohlcv: pd.DataFrame) -> str:
    """Return a stable sha256 fingerprint for validated OHLCV data."""

    normalized = validate_ohlcv(ohlcv)
    frame = normalized.loc[:, REQUIRED_OHLCV_COLUMNS].copy()
    frame.index = frame.index.tz_convert("UTC")
    if not is_datetime64_any_dtype(frame.index):
        raise ValueError("ohlcv index must be datetime-like")
    payload = frame.to_csv(index_label="ts", float_format="%.12g").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
