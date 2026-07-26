"""Point-in-time moving-average and MACD trend strategies."""

from __future__ import annotations

from collections import deque
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
from btc_backtest.strategies.indicators import ema, macd, sma
from btc_backtest.strategies.target_weight import TargetWeightStrategy


class _CrossoverParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fast_window: int = Field(default=50, ge=2, le=1000)
    slow_window: int = Field(default=200, ge=3, le=2000)

    @model_validator(mode="after")
    def validate_windows(self) -> _CrossoverParams:
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be below slow_window")
        return self


class SMACrossoverParams(_CrossoverParams):
    pass


class EMACrossoverParams(_CrossoverParams):
    pass


class MACDTrendParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fast: int = Field(default=12, ge=2, le=500)
    slow: int = Field(default=26, ge=3, le=1000)
    signal: int = Field(default=9, ge=2, le=500)

    @model_validator(mode="after")
    def validate_windows(self) -> MACDTrendParams:
        if self.fast >= self.slow:
            raise ValueError("fast must be below slow")
        return self


class _CrossoverStrategy(TargetWeightStrategy):
    params: _CrossoverParams
    bullish_reason: str
    bearish_reason: str

    def __init__(
        self,
        parameters: Mapping[str, object] | None,
    ) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self._target = Decimal("0")
        self._parameter_values = values

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        close = context.bars["close"]
        fast, slow = self._averages(close)
        if _crossed_above(fast, slow):
            self._target = Decimal("1")
        elif _crossed_below(fast, slow):
            self._target = Decimal("0")
        return self._target

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        if target_value > current_value:
            return self.bullish_reason
        return self.bearish_reason

    def _averages(
        self,
        close: pd.Series,
    ) -> tuple[pd.Series, pd.Series]:
        raise NotImplementedError


class SMACrossover(_CrossoverStrategy):
    metadata = StrategyMetadata(
        id="sma_crossover",
        version="1.0.0",
        description="Long BTC after a confirmed fast SMA cross above slow SMA.",
        warmup_bars=201,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        requires_full_history=False,
        parameter_schema=SMACrossoverParams.model_json_schema(),
    )
    bullish_reason = "sma_bullish_cross"
    bearish_reason = "sma_bearish_cross"
    params: SMACrossoverParams

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        super().__init__(parameters)
        self.params = SMACrossoverParams.model_validate(
            self._parameter_values
        )
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.slow_window + 1}
        )
        self._fast_values: deque[float] = deque(maxlen=self.params.fast_window)
        self._slow_values: deque[float] = deque(maxlen=self.params.slow_window)
        self._fast_sum = 0.0
        self._slow_sum = 0.0
        self._previous_fast: float | None = None
        self._previous_slow: float | None = None
        self._current_fast: float | None = None
        self._current_slow: float | None = None

    def _averages(
        self,
        close: pd.Series,
    ) -> tuple[pd.Series, pd.Series]:
        return (
            sma(close, self.params.fast_window),
            sma(close, self.params.slow_window),
        )

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        self._fast_values.clear()
        self._slow_values.clear()
        self._fast_sum = 0.0
        self._slow_sum = 0.0
        self._previous_fast = None
        self._previous_slow = None
        self._current_fast = None
        self._current_slow = None

    def target_weight(self, context: StrategyContext) -> Decimal:
        close_values = context.bars["close"].to_numpy(dtype="float64", copy=False)
        if not self._slow_values:
            for value in close_values:
                self._append_close(float(value))
        else:
            self._append_close(float(close_values[-1]))

        if (
            self._previous_fast is not None
            and self._previous_slow is not None
            and self._current_fast is not None
            and self._current_slow is not None
        ):
            if (
                self._previous_fast <= self._previous_slow
                and self._current_fast > self._current_slow
            ):
                self._target = Decimal("1")
            elif (
                self._previous_fast >= self._previous_slow
                and self._current_fast < self._current_slow
            ):
                self._target = Decimal("0")
        return self._target

    def _append_close(self, close: float) -> None:
        if not np.isfinite(close):
            raise ValueError("close must be finite")
        self._previous_fast = self._current_fast
        self._previous_slow = self._current_slow
        if len(self._fast_values) == self.params.fast_window:
            self._fast_sum -= self._fast_values[0]
        if len(self._slow_values) == self.params.slow_window:
            self._slow_sum -= self._slow_values[0]
        self._fast_values.append(close)
        self._slow_values.append(close)
        self._fast_sum += close
        self._slow_sum += close
        self._current_fast = (
            self._fast_sum / self.params.fast_window
            if len(self._fast_values) == self.params.fast_window
            else None
        )
        self._current_slow = (
            self._slow_sum / self.params.slow_window
            if len(self._slow_values) == self.params.slow_window
            else None
        )


class EMACrossover(_CrossoverStrategy):
    metadata = StrategyMetadata(
        id="ema_crossover",
        version="1.0.0",
        description="Long BTC after a confirmed fast EMA cross above slow EMA.",
        warmup_bars=201,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=EMACrossoverParams.model_json_schema(),
    )
    bullish_reason = "ema_bullish_cross"
    bearish_reason = "ema_bearish_cross"
    params: EMACrossoverParams

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        super().__init__(parameters)
        self.params = EMACrossoverParams.model_validate(
            self._parameter_values
        )
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.slow_window + 1}
        )

    def _averages(
        self,
        close: pd.Series,
    ) -> tuple[pd.Series, pd.Series]:
        return (
            ema(close, self.params.fast_window),
            ema(close, self.params.slow_window),
        )


class MACDTrend(TargetWeightStrategy):
    metadata = StrategyMetadata(
        id="macd_trend",
        version="1.0.0",
        description="Long BTC while MACD histogram has crossed above zero.",
        warmup_bars=35,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=MACDTrendParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = MACDTrendParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={
                "warmup_bars": self.params.slow + self.params.signal,
            }
        )
        self._target = Decimal("0")

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        _, _, histogram = macd(
            context.bars["close"],
            fast=self.params.fast,
            slow=self.params.slow,
            signal=self.params.signal,
        )
        zero = histogram * 0
        if _crossed_above(histogram, zero):
            self._target = Decimal("1")
        elif _crossed_below(histogram, zero):
            self._target = Decimal("0")
        return self._target

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        if target_value > current_value:
            return "macd_histogram_bullish_cross"
        return "macd_histogram_bearish_cross"


def _parameters(
    parameters: Mapping[str, object] | None,
) -> tuple[dict[str, object], object]:
    values = dict(parameters or {})
    tolerance = values.pop("rebalance_tolerance", Decimal("0.005"))
    return values, tolerance


def _crossed_above(left: pd.Series, right: pd.Series) -> bool:
    left_values = left.to_numpy(dtype="float64")
    right_values = right.to_numpy(dtype="float64")
    if len(left_values) < 2 or len(right_values) < 2:
        return False
    previous_left, current_left = left_values[-2:]
    previous_right, current_right = right_values[-2:]
    if not all(
        np.isfinite(value)
        for value in (
            previous_left,
            current_left,
            previous_right,
            current_right,
        )
    ):
        return False
    return bool(
        previous_left <= previous_right and current_left > current_right
    )


def _crossed_below(left: pd.Series, right: pd.Series) -> bool:
    left_values = left.to_numpy(dtype="float64")
    right_values = right.to_numpy(dtype="float64")
    if len(left_values) < 2 or len(right_values) < 2:
        return False
    previous_left, current_left = left_values[-2:]
    previous_right, current_right = right_values[-2:]
    if not all(
        np.isfinite(value)
        for value in (
            previous_left,
            current_left,
            previous_right,
            current_right,
        )
    ):
        return False
    return bool(
        previous_left >= previous_right and current_left < current_right
    )
