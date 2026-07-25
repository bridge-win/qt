from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from btc_backtest.validation.models import ValidationSpec, Window
from btc_backtest.validation.splits import (
    expanding_splits,
    purged_splits,
    rolling_splits,
)
from pydantic import ValidationError

UTC = timezone.utc


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def test_purged_split_has_no_overlap_and_respects_embargo() -> None:
    index = pd.date_range("2020-01-01", periods=100, freq="1D", tz="UTC")

    split = purged_splits(
        index,
        train_bars=60,
        test_bars=20,
        purge_bars=5,
        embargo_bars=3,
    )[0]

    assert split.train.end <= split.purge.start
    assert split.purge.end <= split.test.start
    assert split.next_eligible_start >= split.test.end + pd.Timedelta(days=3)
    assert set(split.train.timestamps).isdisjoint(split.purge.timestamps)
    assert set(split.train.timestamps).isdisjoint(split.test.timestamps)
    assert set(split.purge.timestamps).isdisjoint(split.test.timestamps)


def test_validation_spec_rejects_test_window_used_for_selection() -> None:
    with pytest.raises(ValidationError, match="final test"):
        ValidationSpec(
            selection_end=utc("2024-12-31"),
            final_test_start=utc("2024-12-01"),
            final_test_end=utc("2025-01-31"),
        )


def test_window_requires_utc_ordered_timestamps() -> None:
    with pytest.raises(ValidationError, match="UTC"):
        Window(
            start=datetime(2024, 1, 1),
            end=utc("2024-01-02"),
            timestamps=(utc("2024-01-01"),),
        )
    with pytest.raises(ValidationError, match="after"):
        Window(
            start=utc("2024-01-02"),
            end=utc("2024-01-01"),
            timestamps=(utc("2024-01-01"),),
        )
    with pytest.raises(ValidationError, match="inside"):
        Window(
            start=utc("2024-01-01"),
            end=utc("2024-01-02"),
            timestamps=(utc("2024-01-02"),),
        )


def test_expanding_splits_keep_fixed_start_and_copy_index() -> None:
    index = pd.date_range("2020-01-01", periods=12, freq="1D", tz="UTC")

    splits = expanding_splits(index, train_bars=4, test_bars=2)

    assert [split.train.start for split in splits] == [
        index[0].to_pydatetime(),
        index[0].to_pydatetime(),
        index[0].to_pydatetime(),
        index[0].to_pydatetime(),
    ]
    assert splits[0].train.timestamps == tuple(index[:4].to_pydatetime())


def test_rolling_splits_keep_fixed_training_length() -> None:
    index = pd.date_range("2020-01-01", periods=12, freq="1D", tz="UTC")

    splits = rolling_splits(index, train_bars=4, test_bars=2)

    assert [len(split.train.timestamps) for split in splits] == [4, 4, 4, 4]
    assert splits[0].train.timestamps == tuple(index[:4].to_pydatetime())
    assert splits[1].train.timestamps == tuple(index[2:6].to_pydatetime())


def test_split_generators_reject_bad_index_or_sizes() -> None:
    naive = pd.date_range("2020-01-01", periods=10, freq="1D")
    duplicate = pd.DatetimeIndex(
        [
            pd.Timestamp("2020-01-01T00:00:00Z"),
            pd.Timestamp("2020-01-01T00:00:00Z"),
        ]
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        rolling_splits(naive, train_bars=4, test_bars=2)
    with pytest.raises(ValueError, match="unique"):
        rolling_splits(duplicate, train_bars=1, test_bars=1)
    with pytest.raises(ValueError, match="positive"):
        purged_splits(
            pd.date_range("2020-01-01", periods=10, freq="1D", tz="UTC"),
            train_bars=0,
            test_bars=2,
            purge_bars=0,
            embargo_bars=0,
        )


def test_insufficient_length_returns_no_splits() -> None:
    index = pd.date_range("2020-01-01", periods=5, freq="1D", tz="UTC")

    assert rolling_splits(index, train_bars=4, test_bars=2) == ()
    assert expanding_splits(index, train_bars=4, test_bars=2) == ()
    assert (
        purged_splits(
            index,
            train_bars=3,
            test_bars=2,
            purge_bars=1,
            embargo_bars=0,
        )
        == ()
    )
