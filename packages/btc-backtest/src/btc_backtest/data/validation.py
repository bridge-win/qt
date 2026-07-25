"""Strict OHLCV normalization, coverage validation, and fingerprints."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import numpy as np
import pandas as pd

from btc_backtest.data.models import DataGap, DataRequest
from btc_backtest.errors import DataCoverageError, DataValidationError

REQUIRED_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
_TIMEFRAME_FREQUENCY = {"1h": "1h", "1d": "1D"}
_TIMEFRAME_DELTA = {"1h": timedelta(hours=1), "1d": timedelta(days=1)}


def _normalize_structure(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        raise DataValidationError("OHLCV data is empty")

    missing_columns = [
        column for column in REQUIRED_OHLCV_COLUMNS if column not in frame.columns
    ]
    if missing_columns:
        raise DataValidationError(
            f"OHLCV data is missing required columns: {', '.join(missing_columns)}"
        )
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise DataValidationError("OHLCV index must be a DatetimeIndex")
    if frame.index.tz is None:
        raise DataValidationError("OHLCV index must be timezone-aware")
    if not frame.index.is_monotonic_increasing:
        raise DataValidationError("OHLCV timestamps must be sorted ascending")
    if not frame.index.is_unique:
        raise DataValidationError("OHLCV data contains duplicate timestamps")

    normalized = frame.loc[:, REQUIRED_OHLCV_COLUMNS].copy()
    normalized.index = normalized.index.tz_convert("UTC")
    normalized.index.name = "timestamp"
    for column in REQUIRED_OHLCV_COLUMNS:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce").astype(
            "float64"
        )

    values = normalized.to_numpy(dtype="float64")
    if not np.isfinite(values).all():
        raise DataValidationError("OHLCV values must be finite")
    if (normalized.loc[:, ("open", "high", "low", "close")] <= 0).any().any():
        raise DataValidationError("OHLCV prices must be positive")
    if (normalized["volume"] < 0).any():
        raise DataValidationError("OHLCV volume must be non-negative")
    if (normalized["high"] < normalized[["open", "close"]].max(axis=1)).any():
        raise DataValidationError("OHLCV high must be at least max(open, close)")
    if (normalized["low"] > normalized[["open", "close"]].min(axis=1)).any():
        raise DataValidationError("OHLCV low must be at most min(open, close)")
    return normalized


def _find_gaps(
    missing: pd.DatetimeIndex,
    timeframe: str,
) -> tuple[DataGap, ...]:
    if missing.empty:
        return ()

    delta = _TIMEFRAME_DELTA[timeframe]
    gaps: list[DataGap] = []
    gap_start = missing[0]
    previous = missing[0]
    count = 1
    for current in missing[1:]:
        if current - previous == delta:
            previous = current
            count += 1
            continue
        gaps.append(
            DataGap(
                start=gap_start.to_pydatetime(),
                end=(previous + delta).to_pydatetime(),
                missing_bars=count,
            )
        )
        gap_start = current
        previous = current
        count = 1
    gaps.append(
        DataGap(
            start=gap_start.to_pydatetime(),
            end=(previous + delta).to_pydatetime(),
            missing_bars=count,
        )
    )
    return tuple(gaps)


def validate_ohlcv(
    frame: pd.DataFrame,
    request: DataRequest,
) -> tuple[pd.DataFrame, tuple[DataGap, ...]]:
    """Normalize OHLCV and enforce the request's point-in-time coverage."""

    normalized = _normalize_structure(frame)
    start = pd.Timestamp(request.start)
    end = pd.Timestamp(request.end)
    outside = (normalized.index < start) | (normalized.index >= end)
    if outside.any():
        first = normalized.index[outside][0]
        raise DataCoverageError(
            f"OHLCV bar {first.isoformat()} is outside requested interval "
            f"[{start.isoformat()}, {end.isoformat()})"
        )

    frequency = _TIMEFRAME_FREQUENCY[request.timeframe]
    if not normalized.index.equals(normalized.index.floor(frequency)):
        raise DataValidationError(
            f"OHLCV timestamps must be aligned to {request.timeframe}"
        )

    expected = pd.date_range(start=start, end=end, freq=frequency, inclusive="left")
    missing = expected.difference(normalized.index)
    gaps = _find_gaps(missing, request.timeframe)
    missing_ratio = len(missing) / len(expected) if len(expected) else 0.0
    if gaps and (request.require_complete or missing_ratio > request.max_missing_ratio):
        first = gaps[0]
        raise DataCoverageError(
            f"missing required bar at {first.start.isoformat()} "
            f"({len(missing)}/{len(expected)} missing, ratio={missing_ratio:.6f})"
        )
    return normalized, gaps


def frame_fingerprint(frame: pd.DataFrame) -> str:
    """Return a stable SHA-256 digest of normalized UTC OHLCV values."""

    normalized = _normalize_structure(frame)
    payload = normalized.to_csv(
        index=True,
        index_label="timestamp",
        date_format="%Y-%m-%dT%H:%M:%S.%fZ",
        float_format="%.12g",
        lineterminator="\n",
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
