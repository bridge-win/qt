from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from time import perf_counter

import numpy as np
import pandas as pd
import pytest
from btc_backtest.acceptance import (
    BITSTAMP_TEN_YEAR_END,
    BITSTAMP_TEN_YEAR_START,
    expected_slots,
    ten_year_request,
)
from btc_backtest.data.models import (
    DataManifest,
    MarketBundle,
    MarketDataset,
    Timeframe,
)
from btc_backtest.data.validation import frame_fingerprint
from btc_backtest.engine.models import BacktestSpec
from btc_backtest.engine.runner import EventRunner
from btc_backtest.strategies.registry import default_strategy_registry

PERFORMANCE_BUDGET_SECONDS = {"1d": 5.0, "1h": 60.0}


@pytest.mark.performance
@pytest.mark.parametrize(("timeframe", "slots"), (("1d", 3652), ("1h", 87648)))
def test_sma_crossover_ten_year_cached_performance(
    timeframe: Timeframe,
    slots: int,
) -> None:
    assert (
        expected_slots(BITSTAMP_TEN_YEAR_START, BITSTAMP_TEN_YEAR_END, timeframe)
        == slots
    )

    bundle = _cached_ten_year_bundle(timeframe)
    strategy = default_strategy_registry().create("sma_crossover", {})
    spec = BacktestSpec(
        strategy="sma_crossover",
        data=ten_year_request(timeframe),
        initial_cash=Decimal("10000"),
        fee_bps=Decimal("1"),
        slippage_bps=Decimal("1"),
        seed=17,
    )

    started = perf_counter()
    result = EventRunner().run(spec, bundle, strategy)
    elapsed = perf_counter() - started

    assert len(result.snapshots) == slots
    assert elapsed < PERFORMANCE_BUDGET_SECONDS[timeframe]


def _cached_ten_year_bundle(timeframe: Timeframe) -> MarketBundle:
    data_request = ten_year_request(timeframe)
    frame = _ohlcv_frame(timeframe)
    manifest = DataManifest(
        provider=data_request.provider,
        market=data_request.market,
        symbol=data_request.symbol,
        timeframe=data_request.timeframe,
        requested_start=data_request.start,
        requested_end=data_request.end,
        delivered_start=data_request.start,
        delivered_end=data_request.end,
        retrieved_at=datetime(2026, 7, 25, tzinfo=BITSTAMP_TEN_YEAR_START.tzinfo),
        real_data=True,
        raw_sha256=("b" * 64,),
        normalized_sha256=frame_fingerprint(frame),
        source="cached-performance-fixture",
    )
    return MarketBundle(
        primary=MarketDataset(frame=frame, manifest=manifest),
        auxiliary={},
    )


def _ohlcv_frame(timeframe: Timeframe) -> pd.DataFrame:
    frequency = "1D" if timeframe == "1d" else "1h"
    index = pd.date_range(
        start=BITSTAMP_TEN_YEAR_START,
        end=BITSTAMP_TEN_YEAR_END,
        freq=frequency,
        inclusive="left",
    )
    step = np.arange(len(index), dtype="float64")
    trend = 450.0 + step * (0.18 if timeframe == "1d" else 0.0075)
    cycle = np.sin(step / 21.0) * 45.0 + np.cos(step / 89.0) * 28.0
    close = trend + cycle
    open_values = np.r_[close[0], close[:-1]]
    spread = 10.0 + np.abs(np.sin(step / 13.0)) * 4.0
    return pd.DataFrame(
        {
            "open": open_values,
            "high": np.maximum(open_values, close) + spread,
            "low": np.minimum(open_values, close) - spread,
            "close": close,
            "volume": 1000.0 + np.abs(np.sin(step / 17.0)) * 100.0,
        },
        index=index,
    )
