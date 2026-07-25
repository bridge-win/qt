"""Closed-open expanding, rolling, and purged time-series splits."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pandas as pd

from btc_backtest.validation.models import ValidationSplit, Window


def expanding_splits(
    index: pd.DatetimeIndex,
    *,
    train_bars: int,
    test_bars: int,
) -> tuple[ValidationSplit, ...]:
    normalized = _validate_index(index)
    _validate_positive(train_bars=train_bars, test_bars=test_bars)
    splits: list[ValidationSplit] = []
    test_start = train_bars
    while test_start + test_bars <= len(normalized):
        train = normalized[:test_start]
        test = normalized[test_start : test_start + test_bars]
        splits.append(
            _split(
                train=train,
                purge=normalized[test_start:test_start],
                test=test,
                embargo=normalized[
                    test_start + test_bars : test_start + test_bars
                ],
                fallback_next=_next_time(normalized, test_start + test_bars),
            )
        )
        test_start += test_bars
    return tuple(splits)


def rolling_splits(
    index: pd.DatetimeIndex,
    *,
    train_bars: int,
    test_bars: int,
) -> tuple[ValidationSplit, ...]:
    normalized = _validate_index(index)
    _validate_positive(train_bars=train_bars, test_bars=test_bars)
    splits: list[ValidationSplit] = []
    train_start = 0
    while train_start + train_bars + test_bars <= len(normalized):
        train_end = train_start + train_bars
        test_end = train_end + test_bars
        splits.append(
            _split(
                train=normalized[train_start:train_end],
                purge=normalized[train_end:train_end],
                test=normalized[train_end:test_end],
                embargo=normalized[test_end:test_end],
                fallback_next=_next_time(normalized, test_end),
            )
        )
        train_start += test_bars
    return tuple(splits)


def purged_splits(
    index: pd.DatetimeIndex,
    *,
    train_bars: int,
    test_bars: int,
    purge_bars: int,
    embargo_bars: int,
) -> tuple[ValidationSplit, ...]:
    normalized = _validate_index(index)
    _validate_positive(train_bars=train_bars, test_bars=test_bars)
    if purge_bars < 0 or embargo_bars < 0:
        raise ValueError("purge_bars and embargo_bars must be non-negative")
    splits: list[ValidationSplit] = []
    train_start = 0
    required = train_bars + purge_bars + test_bars
    while train_start + required <= len(normalized):
        train_end = train_start + train_bars
        purge_end = train_end + purge_bars
        test_end = purge_end + test_bars
        embargo_end = min(test_end + embargo_bars, len(normalized))
        splits.append(
            _split(
                train=normalized[train_start:train_end],
                purge=normalized[train_end:purge_end],
                test=normalized[purge_end:test_end],
                embargo=normalized[test_end:embargo_end],
                fallback_next=_next_time(normalized, embargo_end),
            )
        )
        train_start = embargo_end
    return tuple(splits)


def _validate_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError("validation splits require a DatetimeIndex")
    if index.tz is None:
        raise ValueError("validation index must be timezone-aware")
    normalized = index.tz_convert(timezone.utc)
    if not normalized.is_monotonic_increasing:
        raise ValueError("validation index must be sorted")
    if not normalized.is_unique:
        raise ValueError("validation index must be unique")
    return pd.DatetimeIndex(normalized.copy())


def _validate_positive(
    *,
    train_bars: int,
    test_bars: int,
) -> None:
    if train_bars <= 0 or test_bars <= 0:
        raise ValueError("train_bars and test_bars must be positive")


def _split(
    *,
    train: pd.DatetimeIndex,
    purge: pd.DatetimeIndex,
    test: pd.DatetimeIndex,
    embargo: pd.DatetimeIndex,
    fallback_next: datetime,
) -> ValidationSplit:
    next_eligible = fallback_next
    if len(embargo) > 0:
        embargo_end = _end(embargo)
        if embargo_end is None:
            raise AssertionError("non-empty embargo unexpectedly has no end")
        next_eligible = embargo_end
    return ValidationSplit(
        train=_window(train, fallback_start=None),
        purge=_window(purge, fallback_start=_end(train)),
        test=_window(test, fallback_start=_end(purge) or _end(train)),
        next_eligible_start=next_eligible,
    )


def _window(
    values: pd.DatetimeIndex,
    *,
    fallback_start: datetime | None,
) -> Window:
    timestamps = tuple(
        cast(datetime, item.to_pydatetime())
        for item in values
    )
    if timestamps:
        start = timestamps[0]
        end = _step_end(values)
    elif fallback_start is not None:
        start = fallback_start
        end = fallback_start
    else:
        raise ValueError("non-empty train/test window required")
    return Window(
        start=start,
        end=end,
        timestamps=timestamps,
    )


def _step_end(values: pd.DatetimeIndex) -> datetime:
    last = cast(datetime, values[-1].to_pydatetime())
    step = (
        values[-1] - values[-2]
        if len(values) >= 2
        else pd.Timedelta(days=1)
    )
    return cast(datetime, last + step)


def _end(values: pd.DatetimeIndex) -> datetime | None:
    if len(values) == 0:
        return None
    return _step_end(values)


def _next_time(
    values: pd.DatetimeIndex,
    position: int,
) -> datetime:
    if position < len(values):
        return cast(datetime, values[position].to_pydatetime())
    if len(values) == 0:
        raise ValueError("validation index cannot be empty")
    return _step_end(values)
