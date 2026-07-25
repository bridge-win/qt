from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    MarketBundle,
    MarketDataset,
)
from btc_backtest.engine.models import (
    BacktestSpec,
    InstrumentKind,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from btc_backtest.engine.runner import EventRunner
from btc_backtest.errors import ExecutionError
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)

UTC = timezone.utc


def _timestamp(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _manifest(
    *,
    market: str = "spot",
    timeframe: str = "1d",
    normalized_sha256: str = "b" * 64,
) -> DataManifest:
    return DataManifest.model_validate(
        {
            "provider": "fixture",
            "market": market,
            "symbol": "BTC/USD",
            "timeframe": timeframe,
            "requested_start": _timestamp(1),
            "requested_end": _timestamp(5),
            "delivered_start": _timestamp(1),
            "delivered_end": _timestamp(5),
            "retrieved_at": _timestamp(6),
            "real_data": True,
            "raw_sha256": ("a" * 64,),
            "normalized_sha256": normalized_sha256,
        }
    )


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 110.0, 120.0, 130.0],
            "high": [105.0, 115.0, 125.0, 135.0],
            "low": [95.0, 105.0, 115.0, 125.0],
            "close": [102.0, 112.0, 122.0, 132.0],
            "volume": [10.0, 11.0, 12.0, 13.0],
        },
        index=pd.date_range("2024-01-01", periods=4, freq="1D", tz="UTC"),
    )


def _bundle() -> MarketBundle:
    auxiliary_frame = pd.DataFrame(
        {"rate": [0.001, 0.002]},
        index=pd.DatetimeIndex(
            [
                pd.Timestamp("2024-01-01T12:00:00Z"),
                pd.Timestamp("2024-01-02T12:00:00Z"),
            ]
        ),
    )
    return MarketBundle(
        primary=MarketDataset(frame=_bars(), manifest=_manifest()),
        auxiliary={
            "research": MarketDataset(
                frame=auxiliary_frame,
                manifest=_manifest(
                    timeframe="1h",
                    normalized_sha256="c" * 64,
                ),
            )
        },
    )


def _perpetual_bundle() -> MarketBundle:
    base = _bundle()
    frame = pd.DataFrame(
        {
            "open": [200.0, 210.0, 220.0, 230.0],
            "high": [205.0, 215.0, 225.0, 235.0],
            "low": [195.0, 205.0, 215.0, 225.0],
            "close": [202.0, 212.0, 222.0, 232.0],
            "volume": [20.0, 21.0, 22.0, 23.0],
        },
        index=_bars().index,
    )
    return MarketBundle(
        primary=base.primary,
        auxiliary={
            **base.auxiliary,
            "perpetual": MarketDataset(
                frame=frame,
                manifest=_manifest(
                    market="perpetual",
                    normalized_sha256="e" * 64,
                ),
            ),
        },
    )


def _spec() -> BacktestSpec:
    return BacktestSpec(
        strategy="one_shot",
        data=DataRequest(
            provider="fixture",
            symbol="BTC/USD",
            timeframe="1d",
            start=_timestamp(1),
            end=_timestamp(5),
        ),
        initial_cash=Decimal("1000"),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


class OneShotStrategy:
    metadata = StrategyMetadata(
        id="one_shot",
        version="1.0.0",
        description="Submit one market order.",
        warmup_bars=0,
        supported_timeframes=("1d",),
    )

    def __init__(self) -> None:
        self.initialize_calls = 0
        self.finalize_calls = 0
        self.contexts: list[StrategyContext] = []

    def initialize(self, context: InitializationContext) -> None:
        self.initialize_calls += 1
        self.contexts.clear()

    def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]:
        self.contexts.append(context)
        if len(context.bars) != 1:
            return ()
        return (
            OrderIntent(
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quote_amount=Decimal("102"),
                reason="first bar allocation",
                signal_ids=("signal-1",),
            ),
        )

    def finalize(self, context: FinalizationContext) -> None:
        self.finalize_calls += 1


class AtomicHedgeStrategy:
    metadata = StrategyMetadata(
        id="atomic_hedge",
        version="1.0.0",
        description="Submit a spot/perpetual atomic hedge.",
        warmup_bars=0,
        supported_timeframes=("1d",),
        supported_instruments=(
            InstrumentKind.SPOT,
            InstrumentKind.PERPETUAL,
        ),
    )

    def initialize(self, context: InitializationContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]:
        if len(context.bars) != 1:
            return ()
        return (
            OrderIntent(
                instrument=InstrumentKind.SPOT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                base_quantity=Decimal("1"),
                group_id="hedge",
                atomic_group=True,
                reason="spot leg",
            ),
            OrderIntent(
                instrument=InstrumentKind.PERPETUAL,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                base_quantity=Decimal("1"),
                group_id="hedge",
                atomic_group=True,
                reason="perpetual leg",
            ),
        )

    def finalize(self, context: FinalizationContext) -> None:
        return None


class AtomicLimitStrategy(AtomicHedgeStrategy):
    metadata = AtomicHedgeStrategy.metadata.model_copy(
        update={"id": "atomic_limits"},
    )

    def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]:
        if len(context.bars) != 1:
            return ()
        return (
            OrderIntent(
                instrument=InstrumentKind.SPOT,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                base_quantity=Decimal("1"),
                limit_price=Decimal("115"),
                group_id="limits",
                atomic_group=True,
                reason="reachable spot leg",
            ),
            OrderIntent(
                instrument=InstrumentKind.PERPETUAL,
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                base_quantity=Decimal("1"),
                limit_price=Decimal("1000"),
                group_id="limits",
                atomic_group=True,
                reason="unreachable perpetual leg",
            ),
        )


class BrokenStrategy(OneShotStrategy):
    metadata = OneShotStrategy.metadata.model_copy(
        update={"id": "broken"},
    )

    def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]:
        raise RuntimeError("strategy failed")


def test_runner_is_deterministic_and_uses_next_bar_execution() -> None:
    strategy = OneShotStrategy()

    first = EventRunner().run(_spec(), _bundle(), strategy)
    second = EventRunner().run(_spec(), _bundle(), strategy)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert strategy.initialize_calls == strategy.finalize_calls == 2
    assert len(strategy.contexts) == 4
    assert len(first.orders) == len(first.fills) == 1
    assert first.orders[0].created_at == _timestamp(1)
    assert first.orders[0].status is OrderStatus.FILLED
    assert first.fills[0].timestamp == _timestamp(2)
    assert first.fills[0].price == Decimal("110")
    assert first.snapshots[-1].position(InstrumentKind.SPOT).quantity == 1
    assert first.signal_ids == ("signal-1",)
    assert first.data_manifests == (
        _bundle().primary.manifest,
        _bundle().auxiliary["research"].manifest,
    )


def test_auxiliary_rows_are_gated_by_availability_timestamp() -> None:
    strategy = OneShotStrategy()

    EventRunner().run(_spec(), _bundle(), strategy)

    observed = [
        tuple(frame.index)
        for context in strategy.contexts
        for frame in context.auxiliary.values()
    ]
    assert observed[0] == ()
    assert observed[1] == (pd.Timestamp("2024-01-01T12:00:00Z"),)
    assert observed[2] == (
        pd.Timestamp("2024-01-01T12:00:00Z"),
        pd.Timestamp("2024-01-02T12:00:00Z"),
    )


def test_atomic_group_fills_all_legs_together() -> None:
    spec = _spec().model_copy(update={"strategy": "atomic_hedge"})

    result = EventRunner().run(spec, _perpetual_bundle(), AtomicHedgeStrategy())

    assert len(result.orders) == len(result.fills) == 2
    assert {fill.timestamp for fill in result.fills} == {_timestamp(2)}
    assert {
        fill.instrument: fill.price
        for fill in result.fills
    } == {
        InstrumentKind.SPOT: Decimal("110"),
        InstrumentKind.PERPETUAL: Decimal("210"),
    }
    assert all(order.status is OrderStatus.FILLED for order in result.orders)
    assert result.snapshots[-1].position(InstrumentKind.SPOT).quantity == 1
    assert (
        result.snapshots[-1].position(InstrumentKind.PERPETUAL).quantity
        == Decimal("-1")
    )


def test_atomic_group_does_not_partially_fill_when_one_leg_is_unavailable() -> None:
    spec = _spec().model_copy(update={"strategy": "atomic_limits"})

    result = EventRunner().run(spec, _perpetual_bundle(), AtomicLimitStrategy())

    assert result.fills == ()
    assert all(order.status is OrderStatus.OPEN for order in result.orders)
    assert result.snapshots[-1].position(InstrumentKind.SPOT).quantity == 0
    assert result.snapshots[-1].position(InstrumentKind.PERPETUAL).quantity == 0


def test_available_funding_is_applied_once_to_an_open_perpetual_leg() -> None:
    funding_frame = pd.DataFrame(
        {"rate": [0.01], "mark_price": [115.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-02T12:00:00Z")]),
    )
    base = _perpetual_bundle()
    bundle = MarketBundle(
        primary=base.primary,
        auxiliary={
            "perpetual": base.auxiliary["perpetual"],
            "funding": MarketDataset(
                frame=funding_frame,
                manifest=_manifest(
                    market="perpetual",
                    timeframe="1h",
                    normalized_sha256="d" * 64,
                ),
            )
        },
    )
    spec = _spec().model_copy(update={"strategy": "atomic_hedge"})

    result = EventRunner().run(spec, bundle, AtomicHedgeStrategy())

    perpetual = result.snapshots[-1].position(InstrumentKind.PERPETUAL)
    assert perpetual.funding_pnl == Decimal("1.150")
    assert result.diagnostics["funding_events"] == 1


def test_strategy_exception_is_wrapped_without_partial_result() -> None:
    spec = _spec().model_copy(update={"strategy": "broken"})

    with pytest.raises(
        ExecutionError,
        match=r"broken.*2024-01-01T00:00:00\+00:00",
    ):
        EventRunner().run(spec, _bundle(), BrokenStrategy())


def test_singleton_atomic_group_is_rejected() -> None:
    class InvalidAtomicStrategy(OneShotStrategy):
        metadata = OneShotStrategy.metadata.model_copy(
            update={"id": "invalid_atomic"},
        )

        def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]:
            if len(context.bars) != 1:
                return ()
            return (
                OrderIntent(
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    base_quantity=Decimal("1"),
                    group_id="incomplete",
                    atomic_group=True,
                    reason="missing second leg",
                ),
            )

    spec = _spec().model_copy(update={"strategy": "invalid_atomic"})

    with pytest.raises(ExecutionError, match="atomic group"):
        EventRunner().run(spec, _bundle(), InvalidAtomicStrategy())


def test_strategy_identity_and_timeframe_must_match_spec() -> None:
    mismatch = _spec().model_copy(update={"strategy": "other"})
    with pytest.raises(ExecutionError, match="strategy id"):
        EventRunner().run(mismatch, _bundle(), OneShotStrategy())

    hourly = _spec().model_copy(
        update={
            "data": _spec().data.model_copy(
                update={
                    "timeframe": "1h",
                    "end": _timestamp(1) + timedelta(hours=4),
                }
            )
        }
    )
    with pytest.raises(ExecutionError, match="timeframe"):
        EventRunner().run(hourly, _bundle(), OneShotStrategy())
