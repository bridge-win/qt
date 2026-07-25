"""Shifted Donchian breakout and ATR-sized Turtle strategies."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from btc_backtest.engine.models import InstrumentKind
from btc_backtest.strategies.base import (
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.indicators import atr, donchian
from btc_backtest.strategies.target_weight import TargetWeightStrategy


class DonchianBreakoutParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_window: int = Field(default=20, ge=2, le=2000)
    exit_window: int = Field(default=10, ge=2, le=1000)

    @model_validator(mode="after")
    def validate_windows(self) -> DonchianBreakoutParams:
        if self.exit_window >= self.entry_window:
            raise ValueError("exit_window must be below entry_window")
        return self


class TurtleTrendParams(DonchianBreakoutParams):
    atr_window: int = Field(default=20, ge=2, le=1000)
    risk_fraction: Decimal = Field(default=Decimal("0.01"), gt=0, le=0.1)
    atr_multiple: Decimal = Field(default=Decimal("2"), gt=0, le=20)
    max_weight: Decimal = Field(default=Decimal("1"), gt=0, le=1)


class DonchianBreakout(TargetWeightStrategy):
    params: DonchianBreakoutParams
    metadata = StrategyMetadata(
        id="donchian_breakout",
        version="1.0.0",
        description="Long BTC above the prior long channel; exit the prior short channel.",
        warmup_bars=21,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=DonchianBreakoutParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = DonchianBreakoutParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.entry_window + 1}
        )
        self._target = Decimal("0")

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        _, entry_upper = donchian(
            context.bars["high"],
            context.bars["low"],
            self.params.entry_window,
        )
        exit_lower, _ = donchian(
            context.bars["high"],
            context.bars["low"],
            self.params.exit_window,
        )
        upper = float(entry_upper.shift(1).iloc[-1])
        lower = float(exit_lower.shift(1).iloc[-1])
        close = float(context.current_bar["close"])
        if self._target == 0 and np.isfinite(upper) and close > upper:
            self._target = Decimal("1")
        elif self._target == 1 and np.isfinite(lower) and close < lower:
            self._target = Decimal("0")
        return self._target

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        if target_value > current_value:
            return "donchian_entry_channel"
        return "donchian_exit_channel"


class TurtleTrend(DonchianBreakout):
    params: TurtleTrendParams
    metadata = StrategyMetadata(
        id="turtle_trend",
        version="1.0.0",
        description="ATR-risk-sized Turtle breakout using prior channels.",
        warmup_bars=21,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=TurtleTrendParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        TargetWeightStrategy.__init__(
            self,
            {"rebalance_tolerance": tolerance},
        )
        self.params = TurtleTrendParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={
                "warmup_bars": max(
                    self.params.entry_window,
                    self.params.atr_window,
                )
                + 1,
                "max_weight": self.params.max_weight,
            }
        )
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        _, entry_upper = donchian(
            context.bars["high"],
            context.bars["low"],
            self.params.entry_window,
        )
        exit_lower, _ = donchian(
            context.bars["high"],
            context.bars["low"],
            self.params.exit_window,
        )
        upper = float(entry_upper.shift(1).iloc[-1])
        lower = float(exit_lower.shift(1).iloc[-1])
        close_float = float(context.current_bar["close"])
        if self._target == 0 and np.isfinite(upper) and close_float > upper:
            range_value = float(
                atr(
                    context.bars["high"],
                    context.bars["low"],
                    context.bars["close"],
                    self.params.atr_window,
                ).iloc[-1]
            )
            if not np.isfinite(range_value) or range_value <= 0:
                return self._target
            close = Decimal(str(close_float))
            stop_distance = Decimal(str(range_value)) * self.params.atr_multiple
            risk_budget = context.portfolio.equity * self.params.risk_fraction
            notional = risk_budget / stop_distance * close
            self._target = min(
                self.params.max_weight,
                notional / context.portfolio.equity,
            )
        elif self._target > 0 and np.isfinite(lower) and close_float < lower:
            self._target = Decimal("0")
        return self._target

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        if target_value > current_value:
            return "turtle_entry_channel"
        return "turtle_exit_channel"


def _parameters(
    parameters: Mapping[str, object] | None,
) -> tuple[dict[str, object], object]:
    values = dict(parameters or {})
    tolerance = values.pop("rebalance_tolerance", Decimal("0.005"))
    return values, tolerance
