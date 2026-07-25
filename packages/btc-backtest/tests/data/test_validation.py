from collections.abc import Callable
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from btc_backtest.data.models import DataRequest
from btc_backtest.data.validation import frame_fingerprint, validate_ohlcv
from btc_backtest.errors import DataCoverageError, DataValidationError
from hypothesis import given, settings
from hypothesis import strategies as st

UTC = timezone.utc


def request(
    *,
    require_complete: bool = True,
    max_missing_ratio: float = 0.0,
) -> DataRequest:
    return DataRequest(
        provider="fixture",
        symbol="BTC/USD",
        timeframe="1d",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 4, tzinfo=UTC),
        require_complete=require_complete,
        max_missing_ratio=max_missing_ratio,
    )


def valid_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": ["10", "11", "12"],
            "high": [12, 13, 14],
            "low": [9, 10, 11],
            "close": [11, 12, 13],
            "volume": [1, 2, 3],
        },
        index=pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC"),
    )


def test_validation_normalizes_numeric_ohlcv_and_fingerprints() -> None:
    normalized, gaps = validate_ohlcv(valid_frame(), request())

    assert normalized.dtypes.tolist() == ["float64"] * 5
    assert gaps == ()
    assert frame_fingerprint(normalized) == frame_fingerprint(normalized.copy())


def test_fingerprint_changes_when_market_data_changes() -> None:
    normalized, _ = validate_ohlcv(valid_frame(), request())
    changed = normalized.copy()
    changed.loc[pd.Timestamp("2024-01-02", tz="UTC"), "close"] = 12.5

    assert frame_fingerprint(changed) != frame_fingerprint(normalized)


def test_validation_reports_missing_bar_when_incomplete_data_is_allowed() -> None:
    frame = valid_frame().drop(pd.Timestamp("2024-01-02", tz="UTC"))

    normalized, gaps = validate_ohlcv(
        frame,
        request(require_complete=False, max_missing_ratio=0.5),
    )

    assert len(normalized) == 2
    assert len(gaps) == 1
    assert gaps[0].start == datetime(2024, 1, 2, tzinfo=UTC)
    assert gaps[0].end == datetime(2024, 1, 3, tzinfo=UTC)
    assert gaps[0].missing_bars == 1


def test_validation_rejects_missing_required_bar() -> None:
    frame = valid_frame().drop(pd.Timestamp("2024-01-02", tz="UTC"))

    with pytest.raises(DataCoverageError, match="2024-01-02"):
        validate_ohlcv(frame, request())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda frame: frame.drop(columns="volume"), "missing required columns"),
        (lambda frame: frame.iloc[::-1], "sorted ascending"),
        (
            lambda frame: pd.concat([frame.iloc[[0]], frame]),
            "duplicate timestamps",
        ),
        (
            lambda frame: frame.set_axis(
                frame.index.tz_localize(None),
                axis="index",
            ),
            "timezone-aware",
        ),
        (
            lambda frame: frame.assign(close=[11, np.inf, 13]),
            "finite",
        ),
        (
            lambda frame: frame.assign(high=[12, 11, 14]),
            "high",
        ),
        (
            lambda frame: frame.assign(low=[9, 13, 11]),
            "low",
        ),
        (
            lambda frame: frame.assign(volume=[1, -1, 3]),
            "volume",
        ),
    ],
)
def test_validation_rejects_structurally_invalid_data(
    mutate: Callable[[pd.DataFrame], pd.DataFrame],
    message: str,
) -> None:
    with pytest.raises(DataValidationError, match=message):
        validate_ohlcv(mutate(valid_frame()), request())


def test_validation_rejects_bar_outside_closed_open_interval() -> None:
    frame = pd.concat(
        [
            valid_frame(),
            pd.DataFrame(
                {"open": [13], "high": [15], "low": [12], "close": [14], "volume": [4]},
                index=pd.DatetimeIndex(["2024-01-04"], tz="UTC"),
            ),
        ]
    )

    with pytest.raises(DataCoverageError, match="outside requested interval"):
        validate_ohlcv(frame, request())


@given(
    bars=st.lists(
        st.tuples(
            st.integers(min_value=10, max_value=1_000_000),
            st.integers(min_value=-5, max_value=5),
            st.integers(min_value=0, max_value=4),
            st.integers(min_value=0, max_value=1_000_000_000),
        ),
        min_size=1,
        max_size=50,
    )
)
@settings(max_examples=50, deadline=None)
def test_valid_daily_bars_preserve_complete_coverage(
    bars: list[tuple[int, int, int, int]],
) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    index = pd.date_range(start, periods=len(bars), freq="1D")
    rows: list[dict[str, int]] = []
    for open_price, change, cushion, volume in bars:
        close = open_price + change
        rows.append(
            {
                "open": open_price,
                "high": max(open_price, close) + cushion,
                "low": min(open_price, close) - cushion,
                "close": close,
                "volume": volume,
            }
        )
    frame = pd.DataFrame(rows, index=index)
    data_request = DataRequest(
        provider="fixture",
        symbol="BTC/USD",
        timeframe="1d",
        start=start,
        end=start + pd.Timedelta(days=len(bars)),
    )

    normalized, gaps = validate_ohlcv(frame, data_request)

    assert normalized.index.equals(index.rename("timestamp"))
    assert gaps == ()
    assert len(frame_fingerprint(normalized)) == 64
