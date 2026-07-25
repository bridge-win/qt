"""Reference external strategy loaded through the public file-path API."""

from decimal import Decimal

from btc_backtest.engine.models import OrderIntent, OrderSide, OrderType
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)


class CustomStrategy:
    metadata = StrategyMetadata(
        id="custom_sma",
        version="1.0.0",
        api_version="1",
        description="Minimal fast/slow SMA crossover example.",
        warmup_bars=3,
        supported_timeframes=("1h", "1d"),
        required_fields=("close",),
    )

    def initialize(self, context: InitializationContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        if len(context.bars) < self.metadata.warmup_bars:
            return ()
        close = context.bars["close"]
        fast = float(close.iloc[-2:].mean())
        slow = float(close.iloc[-3:].mean())
        spot = context.portfolio.position("spot")
        if fast > slow and spot.quantity == 0:
            return (
                OrderIntent(
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quote_amount=Decimal("100"),
                    reason="fast SMA crossed above slow SMA",
                ),
            )
        if fast < slow and spot.quantity > 0:
            return (
                OrderIntent(
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    base_quantity=spot.quantity,
                    reason="fast SMA crossed below slow SMA",
                ),
            )
        return ()

    def finalize(self, context: FinalizationContext) -> None:
        return None
