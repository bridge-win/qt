"""RSI and stochastic long/flat reversal strategies."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from btc_backtest.engine.models import InstrumentKind
from btc_backtest.strategies.base import (
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.indicators import rsi, stochastic
from btc_backtest.strategies.target_weight import TargetWeightStrategy


class RSIMeanReversionParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: int = Field(default=14, ge=2, le=500)
    entry: Decimal = Field(default=Decimal("30"), ge=0, le=100)
    exit: Decimal = Field(default=Decimal("55"), ge=0, le=100)

    @model_validator(mode="after")
    def validate_levels(self) -> RSIMeanReversionParams:
        if self.entry >= self.exit:
            raise ValueError("RSI entry must be below exit")
        return self


class StochasticReversalParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    k_window: int = Field(default=14, ge=2, le=500)
    d_window: int = Field(default=3, ge=2, le=100)
    entry: Decimal = Field(default=Decimal("20"), ge=0, le=100)
    exit: Decimal = Field(default=Decimal("80"), ge=0, le=100)

    @model_validator(mode="after")
    def validate_levels(self) -> StochasticReversalParams:
        if self.entry >= self.exit:
            raise ValueError("stochastic entry must be below exit")
        return self


class _OscillatorStrategy(TargetWeightStrategy):
    _target: Decimal
    entry_reason: str
    exit_reason: str

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        self._target = Decimal("0")

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        if target_value > current_value:
            return self.entry_reason
        return self.exit_reason


class RSIMeanReversion(_OscillatorStrategy):
    metadata = StrategyMetadata(
        id="rsi_mean_reversion",
        version="1.0.0",
        description="Buy oversold RSI and exit after normalization.",
        warmup_bars=15,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=RSIMeanReversionParams.model_json_schema(),
    )
    entry_reason = "rsi_oversold_entry"
    exit_reason = "rsi_normalization_exit"

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = RSIMeanReversionParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.window + 1}
        )
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        value = rsi(context.bars["close"], self.params.window).iloc[-1]
        if not np.isfinite(value):
            return self._target
        current = Decimal(str(float(value)))
        if self._target == 0 and current <= self.params.entry:
            self._target = Decimal("1")
        elif self._target == 1 and current >= self.params.exit:
            self._target = Decimal("0")
        return self._target


class StochasticReversal(_OscillatorStrategy):
    metadata = StrategyMetadata(
        id="stochastic_reversal",
        version="1.0.0",
        description="Trade completed stochastic crosses in extreme zones.",
        warmup_bars=17,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=StochasticReversalParams.model_json_schema(),
    )
    entry_reason = "stochastic_oversold_cross"
    exit_reason = "stochastic_overbought_cross"

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = StochasticReversalParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={
                "warmup_bars": (
                    self.params.k_window + self.params.d_window
                )
            }
        )
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        percent_k, percent_d = stochastic(
            context.bars["high"],
            context.bars["low"],
            context.bars["close"],
            self.params.k_window,
            self.params.d_window,
        )
        if self._target == 0 and _crossed_above(
            percent_k,
            percent_d,
            upper_bound=float(self.params.entry),
        ):
            self._target = Decimal("1")
        elif self._target == 1 and _crossed_below(
            percent_k,
            percent_d,
            lower_bound=float(self.params.exit),
        ):
            self._target = Decimal("0")
        return self._target


def _parameters(
    parameters: Mapping[str, object] | None,
) -> tuple[dict[str, object], object]:
    values = dict(parameters or {})
    tolerance = values.pop("rebalance_tolerance", Decimal("0.005"))
    return values, tolerance


def _crossed_above(
    left: pd.Series,
    right: pd.Series,
    *,
    upper_bound: float,
) -> bool:
    values = _last_two(left, right)
    if values is None:
        return False
    previous_left, current_left, previous_right, current_right = values
    return bool(
        previous_left <= previous_right
        and current_left > current_right
        and current_left <= upper_bound
        and current_right <= upper_bound
    )


def _crossed_below(
    left: pd.Series,
    right: pd.Series,
    *,
    lower_bound: float,
) -> bool:
    values = _last_two(left, right)
    if values is None:
        return False
    previous_left, current_left, previous_right, current_right = values
    return bool(
        previous_left >= previous_right
        and current_left < current_right
        and current_left >= lower_bound
        and current_right >= lower_bound
    )


def _last_two(
    left: pd.Series,
    right: pd.Series,
) -> tuple[float, float, float, float] | None:
    if len(left) < 2 or len(right) < 2:
        return None
    values = (
        float(left.iloc[-2]),
        float(left.iloc[-1]),
        float(right.iloc[-2]),
        float(right.iloc[-1]),
    )
    if not all(np.isfinite(value) for value in values):
        return None
    return values
