from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from btc_backtest.api import BacktestRunner
from btc_backtest.data.cache import DataCache
from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    MarketBundle,
    MarketDataset,
)
from btc_backtest.data.providers.base import ProviderMetadata
from btc_backtest.data.validation import frame_fingerprint
from btc_backtest.engine.models import (
    BacktestResult,
    BacktestSpec,
    OrderIntent,
    OrderSide,
    OrderType,
)
from btc_backtest.engine.runner import EventRunner
from btc_backtest.errors import StrategyLoadError
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    Strategy,
    StrategyContext,
    StrategyMetadata,
)

UTC = timezone.utc


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [10.0, 11.0, 12.0],
        },
        index=pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC"),
    )


def _request(*, market: str = "spot") -> DataRequest:
    return DataRequest(
        provider="fixture",
        symbol="BTC/USD",
        timeframe="1d",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 4, tzinfo=UTC),
        market=market,
    )


class FixtureProvider:
    metadata = ProviderMetadata(
        id="fixture",
        real_data=True,
        timeframes=("1d",),
        markets=("spot", "research"),
        symbols=("BTC/USD",),
    )

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, request: DataRequest) -> MarketDataset:
        self.calls += 1
        frame = _bars()
        fingerprint = frame_fingerprint(frame)
        return MarketDataset(
            frame=frame,
            manifest=DataManifest(
                provider="fixture",
                market=request.market,
                symbol=request.symbol,
                timeframe=request.timeframe,
                requested_start=request.start,
                requested_end=request.end,
                delivered_start=request.start,
                delivered_end=request.end,
                retrieved_at=request.end,
                real_data=True,
                raw_sha256=("a" * 64,),
                normalized_sha256=fingerprint,
            ),
        )


class FixtureStrategy:
    metadata = StrategyMetadata(
        id="fixture_strategy",
        version="1.0.0",
        description="Fixture public API strategy.",
        warmup_bars=0,
        supported_timeframes=("1d",),
    )

    def initialize(self, context: InitializationContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]:
        if len(context.bars) != 1:
            return ()
        return (
            OrderIntent(
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quote_amount=Decimal("100"),
                reason="fixture allocation",
            ),
        )

    def finalize(self, context: FinalizationContext) -> None:
        return None


class CountingEngine(EventRunner):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run(
        self,
        spec: BacktestSpec,
        bundle: MarketBundle,
        strategy: Strategy,
    ) -> BacktestResult:
        self.calls += 1
        return super().run(spec, bundle, strategy)


def _spec() -> BacktestSpec:
    return BacktestSpec(
        strategy="fixture_strategy",
        data=_request(),
        initial_cash=Decimal("1000"),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


def test_public_runner_fetches_caches_and_executes(tmp_path: Path) -> None:
    provider = FixtureProvider()
    engine = CountingEngine()
    runner = BacktestRunner(
        provider_registry={"fixture": provider},
        strategy_registry={"fixture_strategy": FixtureStrategy},
        cache=DataCache(tmp_path),
        engine=engine,
    )

    first = runner.run(_spec())
    second = runner.run(_spec(), strategy=FixtureStrategy())

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.data_manifests[0].provider == _spec().data.provider
    assert first.snapshots
    assert provider.calls == 1
    assert engine.calls == 2


def test_public_runner_fetches_named_auxiliary_requests(tmp_path: Path) -> None:
    provider = FixtureProvider()
    runner = BacktestRunner(
        provider_registry={"fixture": provider},
        strategy_registry={},
        cache=DataCache(tmp_path),
    )
    spec = _spec().model_copy(
        update={"auxiliary_data": (_request(market="research"),)}
    )

    result = runner.run(spec, strategy=FixtureStrategy())

    assert [manifest.market for manifest in result.data_manifests] == [
        "spot",
        "research",
    ]
    assert provider.calls == 2


def test_public_runner_rejects_unknown_strategy(tmp_path: Path) -> None:
    runner = BacktestRunner(
        provider_registry={"fixture": FixtureProvider()},
        strategy_registry={},
        cache=DataCache(tmp_path),
    )

    with pytest.raises(StrategyLoadError, match="unknown strategy"):
        runner.run(_spec())
