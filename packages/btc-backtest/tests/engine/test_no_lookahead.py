from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    MarketBundle,
    MarketDataset,
)
from btc_backtest.engine.models import (
    BacktestResult,
    BacktestSpec,
    OrderIntent,
    OrderSide,
    OrderType,
)
from btc_backtest.engine.runner import EventRunner
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)

UTC = timezone.utc


class ThresholdStrategy:
    metadata = StrategyMetadata(
        id="threshold",
        version="1.0.0",
        description="Buy after the visible close crosses a threshold.",
        warmup_bars=0,
        supported_timeframes=("1d",),
    )

    def __init__(self) -> None:
        self.visible_maxima: list[tuple[datetime, datetime]] = []
        self.submitted = False

    def initialize(self, context: InitializationContext) -> None:
        self.visible_maxima.clear()
        self.submitted = False

    def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]:
        latest = context.bars.index.max().to_pydatetime()
        self.visible_maxima.append((context.timestamp, latest))
        if self.submitted or float(context.current_bar["close"]) <= 102:
            return ()
        self.submitted = True
        return (
            OrderIntent(
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quote_amount=Decimal("100"),
                reason="visible threshold",
            ),
        )

    def finalize(self, context: FinalizationContext) -> None:
        return None


def _bundle(frame: pd.DataFrame) -> MarketBundle:
    start = frame.index[0].to_pydatetime()
    end = (frame.index[-1] + pd.Timedelta(days=1)).to_pydatetime()
    manifest = DataManifest(
        provider="fixture",
        market="spot",
        symbol="BTC/USD",
        timeframe="1d",
        requested_start=start,
        requested_end=end,
        delivered_start=start,
        delivered_end=end,
        retrieved_at=end,
        real_data=True,
        raw_sha256=("a" * 64,),
        normalized_sha256="b" * 64,
    )
    return MarketBundle(
        primary=MarketDataset(frame=frame, manifest=manifest),
        auxiliary={},
    )


def _spec(frame: pd.DataFrame) -> BacktestSpec:
    return BacktestSpec(
        strategy="threshold",
        data=DataRequest(
            provider="fixture",
            symbol="BTC/USD",
            timeframe="1d",
            start=frame.index[0].to_pydatetime(),
            end=(frame.index[-1] + pd.Timedelta(days=1)).to_pydatetime(),
        ),
        initial_cash=Decimal("1000"),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


def _events_through(
    result: BacktestResult,
    cutoff: datetime,
) -> tuple[tuple[object, ...], ...]:
    return (
        tuple(order for order in result.orders if order.created_at <= cutoff),
        tuple(fill for fill in result.fills if fill.timestamp <= cutoff),
        tuple(
            snapshot
            for snapshot in result.snapshots
            if snapshot.timestamp <= cutoff
        ),
    )


def test_future_bar_mutation_cannot_change_past_events() -> None:
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0, 106.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0],
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
            "volume": [10.0] * 6,
        },
        index=pd.date_range("2024-01-01", periods=6, freq="1D", tz="UTC"),
    )
    cutoff = frame.index[2].to_pydatetime()
    mutated = frame.copy()
    mutated.loc[mutated.index > cutoff, ["open", "high", "low", "close"]] *= 10
    first_strategy = ThresholdStrategy()
    second_strategy = ThresholdStrategy()

    first = EventRunner().run(_spec(frame), _bundle(frame), first_strategy)
    second = EventRunner().run(
        _spec(mutated),
        _bundle(mutated),
        second_strategy,
    )

    assert _events_through(first, cutoff) == _events_through(second, cutoff)
    assert all(active == latest for active, latest in first_strategy.visible_maxima)
    assert all(active == latest for active, latest in second_strategy.visible_maxima)
