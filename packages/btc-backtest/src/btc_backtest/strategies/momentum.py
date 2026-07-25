"""Absolute, dual, rate-of-change, and ADX momentum strategies."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from btc_backtest.engine.models import InstrumentKind
from btc_backtest.strategies.base import StrategyContext, StrategyMetadata
from btc_backtest.strategies.indicators import adx, roc
from btc_backtest.strategies.target_weight import TargetWeightStrategy


class TimeSeriesMomentumParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    lookback: int = Field(default=90, ge=2, le=2000)


class DualMomentumParams(TimeSeriesMomentumParams):
    cash_annual_rate: Decimal = Field(default=Decimal("0.03"), ge=0, le=1)


class RateOfChangeParams(TimeSeriesMomentumParams):
    entry: Decimal = Field(default=Decimal("0.05"), ge=0, le=10)
    exit: Decimal = Field(default=Decimal("0"), ge=-1, le=10)

    @model_validator(mode="after")
    def validate_levels(self) -> RateOfChangeParams:
        if self.exit >= self.entry:
            raise ValueError("ROC exit must be below entry")
        return self


class ADXTrendParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    window: int = Field(default=14, ge=2, le=500)
    threshold: Decimal = Field(default=Decimal("25"), ge=0, le=100)


class _MomentumStrategy(TargetWeightStrategy):
    entry_reason: str
    exit_reason: str

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        if target_value > current_value:
            return self.entry_reason
        return self.exit_reason


class TimeSeriesMomentum(_MomentumStrategy):
    metadata = StrategyMetadata(
        id="time_series_momentum",
        version="1.0.0",
        description="Long BTC when its trailing absolute return is positive.",
        warmup_bars=91,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=TimeSeriesMomentumParams.model_json_schema(),
    )
    entry_reason = "positive_time_series_momentum"
    exit_reason = "negative_time_series_momentum"

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = TimeSeriesMomentumParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.lookback + 1}
        )

    def target_weight(self, context: StrategyContext) -> Decimal:
        value = float(roc(context.bars["close"], self.params.lookback).iloc[-1])
        if not np.isfinite(value):
            return Decimal("0")
        return Decimal("1") if value > 0 else Decimal("0")


class DualMomentum(_MomentumStrategy):
    metadata = StrategyMetadata(
        id="dual_momentum",
        version="1.0.0",
        description="Require positive BTC momentum above a cash-rate hurdle.",
        warmup_bars=91,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=DualMomentumParams.model_json_schema(),
    )
    entry_reason = "dual_momentum_outperformance"
    exit_reason = "dual_momentum_cash_preference"

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = DualMomentumParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.lookback + 1}
        )

    def target_weight(self, context: StrategyContext) -> Decimal:
        value = float(roc(context.bars["close"], self.params.lookback).iloc[-1])
        if not np.isfinite(value):
            return Decimal("0")
        start = context.bars.index[-self.params.lookback - 1]
        end = context.bars.index[-1]
        years = (end - start).total_seconds() / (365.2425 * 86_400)
        hurdle = (1 + float(self.params.cash_annual_rate)) ** years - 1
        return Decimal("1") if value > max(0.0, hurdle) else Decimal("0")


class RateOfChange(_MomentumStrategy):
    metadata = StrategyMetadata(
        id="rate_of_change",
        version="1.0.0",
        description="ROC entry and exit hysteresis.",
        warmup_bars=91,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=RateOfChangeParams.model_json_schema(),
    )
    entry_reason = "roc_entry_threshold"
    exit_reason = "roc_exit_threshold"

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = RateOfChangeParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.lookback + 1}
        )
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        value = float(roc(context.bars["close"], self.params.lookback).iloc[-1])
        if not np.isfinite(value):
            return self._target
        current = Decimal(str(value))
        if self._target == 0 and current >= self.params.entry:
            self._target = Decimal("1")
        elif self._target == 1 and current <= self.params.exit:
            self._target = Decimal("0")
        return self._target


class ADXTrend(_MomentumStrategy):
    metadata = StrategyMetadata(
        id="adx_trend",
        version="1.0.0",
        description="Long BTC only with positive direction and strong ADX.",
        warmup_bars=28,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=ADXTrendParams.model_json_schema(),
    )
    entry_reason = "adx_positive_trend"
    exit_reason = "adx_trend_failure"

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = ADXTrendParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.window * 2}
        )

    def target_weight(self, context: StrategyContext) -> Decimal:
        strength, plus_di, minus_di = adx(
            context.bars["high"],
            context.bars["low"],
            context.bars["close"],
            self.params.window,
        )
        values = (
            float(strength.iloc[-1]),
            float(plus_di.iloc[-1]),
            float(minus_di.iloc[-1]),
        )
        if not all(np.isfinite(value) for value in values):
            return Decimal("0")
        strong, positive, negative = values
        if strong >= float(self.params.threshold) and positive > negative:
            return Decimal("1")
        return Decimal("0")


def _parameters(
    parameters: Mapping[str, object] | None,
) -> tuple[dict[str, object], object]:
    values = dict(parameters or {})
    tolerance = values.pop("rebalance_tolerance", Decimal("0.005"))
    return values, tolerance
