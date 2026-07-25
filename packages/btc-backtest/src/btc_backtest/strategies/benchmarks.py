"""Independent benchmark strategies."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from btc_backtest.engine.models import OrderIntent
from btc_backtest.strategies.base import (
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.target_weight import TargetWeightStrategy


class BuyAndHoldParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allocation: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    rebalance_tolerance: Decimal = Field(
        default=Decimal("0.005"),
        ge=0,
        le=1,
    )


class BuyAndHold(TargetWeightStrategy):
    metadata = StrategyMetadata(
        id="buy_and_hold",
        version="1.0.0",
        description="Buy the configured BTC allocation once and hold it.",
        warmup_bars=0,
        supported_timeframes=("1h", "1d"),
        parameter_schema=BuyAndHoldParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        self.params = BuyAndHoldParams.model_validate(parameters or {})
        super().__init__(
            {"rebalance_tolerance": self.params.rebalance_tolerance}
        )
        self._submitted = False

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        self._submitted = False

    def target_weight(self, context: StrategyContext) -> Decimal:
        return self.params.allocation

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        if self._submitted:
            return ()
        intents = super().on_bar(context)
        if intents:
            self._submitted = True
        return intents

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        return "initial_buy"
