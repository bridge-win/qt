"""Fixture strategy package for entry-point contract tests."""

from btc_backtest.engine.models import OrderIntent
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)


class ExternalFixtureStrategy:
    metadata = StrategyMetadata(
        id="external_fixture",
        version="1.0.0",
        api_version="1",
        description="External fixture strategy.",
        warmup_bars=0,
        supported_timeframes=("1d",),
    )

    def initialize(self, context: InitializationContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        return ()

    def finalize(self, context: FinalizationContext) -> None:
        return None
