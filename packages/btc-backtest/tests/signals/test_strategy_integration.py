from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    MarketBundle,
    MarketDataset,
)
from btc_backtest.engine.models import (
    BacktestSpec,
    OrderIntent,
    OrderSide,
    OrderType,
)
from btc_backtest.engine.runner import EventRunner
from btc_backtest.signals.store import SignalStore
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)

from .helpers import observation, utc

UTC = timezone.utc


class SignalAwareStrategy:
    metadata = StrategyMetadata(
        id="signal_aware",
        version="1",
        description="Uses ranked network signals.",
        warmup_bars=2,
        supported_timeframes=("1d",),
        signal_dependencies=("sentiment",),
    )

    def __init__(self) -> None:
        self.seen_signal_ids: tuple[str, ...] = ()
        self._ordered = False

    def initialize(self, context: InitializationContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]:
        if not self.seen_signal_ids:
            self.seen_signal_ids = tuple(
                contributor.observation_id
                for ranked in context.signals
                for contributor in ranked.contributors
            )
        if self._ordered or not context.signals:
            return ()
        self._ordered = True
        return (
            OrderIntent(
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quote_amount=Decimal("100"),
                reason="follow signal",
            ),
        )

    def finalize(self, context: FinalizationContext) -> None:
        return None


def test_strategy_sees_only_declared_point_in_time_signals(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path / "signals")
    early = observation(
        id="early",
        source_event_id="early",
        observed_at=utc(days=1),
        effective_at=utc(days=1),
        expires_at=utc(days=4),
        payload_sha256="1" * 64,
    )
    late = observation(
        id="late",
        source_event_id="late",
        observed_at=utc(days=2),
        effective_at=utc(days=1),
        expires_at=utc(days=4),
        payload_sha256="2" * 64,
    )
    store.append((early, late))
    strategy = SignalAwareStrategy()

    result = EventRunner(signal_store=store).run(
        spec(),
        bundle(),
        strategy,
    )

    assert strategy.seen_signal_ids == (early.id,)
    assert result.orders[0].signal_ids == (early.id,)
    assert late.id not in result.signal_ids


def spec() -> BacktestSpec:
    return BacktestSpec(
        strategy="signal_aware",
        data=DataRequest(
            provider="fixture",
            symbol="BTC/USD",
            timeframe="1d",
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 4, tzinfo=UTC),
            require_real=False,
        ),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


def bundle() -> MarketBundle:
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.0, 101.0, 102.0],
            "volume": [10.0, 11.0, 12.0],
        },
        index=pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC"),
    )
    manifest = DataManifest(
        provider="fixture",
        market="spot",
        symbol="BTC/USD",
        timeframe="1d",
        requested_start=datetime(2024, 1, 1, tzinfo=UTC),
        requested_end=datetime(2024, 1, 4, tzinfo=UTC),
        delivered_start=datetime(2024, 1, 1, tzinfo=UTC),
        delivered_end=datetime(2024, 1, 4, tzinfo=UTC),
        retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
        real_data=False,
        raw_sha256=("0" * 64,),
        normalized_sha256="1" * 64,
    )
    return MarketBundle(
        primary=MarketDataset(frame=frame, manifest=manifest),
        auxiliary={},
    )
