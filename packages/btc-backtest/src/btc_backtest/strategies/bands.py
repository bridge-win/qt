"""Bollinger mean-reversion and volatility-breakout strategies."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from btc_backtest.engine.models import InstrumentKind
from btc_backtest.strategies.base import (
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.indicators import atr, bollinger
from btc_backtest.strategies.target_weight import TargetWeightStrategy


class BollingerMeanReversionParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: int = Field(default=20, ge=2, le=1000)
    stddev: Decimal = Field(default=Decimal("2"), gt=0, le=10)


class BollingerBreakoutParams(BollingerMeanReversionParams):
    atr_window: int = Field(default=14, ge=2, le=1000)
    atr_stop: Decimal = Field(default=Decimal("2"), gt=0, le=20)


class _BandStrategy(TargetWeightStrategy):
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


class BollingerMeanReversion(_BandStrategy):
    metadata = StrategyMetadata(
        id="bollinger_mean_reversion",
        version="1.0.0",
        description="Buy the lower Bollinger band and exit at the center.",
        warmup_bars=20,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=BollingerMeanReversionParams.model_json_schema(),
    )
    entry_reason = "lower_band_entry"
    exit_reason = "bollinger_center_exit"

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = BollingerMeanReversionParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.window}
        )
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        lower, middle, _ = bollinger(
            context.bars["close"],
            self.params.window,
            float(self.params.stddev),
        )
        close = float(context.current_bar["close"])
        lower_value = float(lower.iloc[-1])
        middle_value = float(middle.iloc[-1])
        if not all(np.isfinite(item) for item in (close, lower_value, middle_value)):
            return self._target
        if self._target == 0 and close <= lower_value:
            self._target = Decimal("1")
        elif self._target == 1 and close >= middle_value:
            self._target = Decimal("0")
        return self._target


class BollingerBreakout(_BandStrategy):
    metadata = StrategyMetadata(
        id="bollinger_breakout",
        version="1.0.0",
        description="Buy an upper-band breakout with a rising ATR stop.",
        warmup_bars=21,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=BollingerBreakoutParams.model_json_schema(),
    )
    entry_reason = "upper_band_breakout"
    exit_reason = "atr_trailing_exit"

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = BollingerBreakoutParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={
                "warmup_bars": max(
                    self.params.window,
                    self.params.atr_window,
                )
                + 1
            }
        )
        self._target = Decimal("0")
        self._highest_close: Decimal | None = None
        self.trailing_stop: Decimal | None = None

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        self._highest_close = None
        self.trailing_stop = None

    def target_weight(self, context: StrategyContext) -> Decimal:
        _, _, upper = bollinger(
            context.bars["close"],
            self.params.window,
            float(self.params.stddev),
        )
        average_range = atr(
            context.bars["high"],
            context.bars["low"],
            context.bars["close"],
            self.params.atr_window,
        )
        close_float = float(context.current_bar["close"])
        upper_value = float(upper.iloc[-1])
        range_value = float(average_range.iloc[-1])
        if not all(
            np.isfinite(item)
            for item in (close_float, upper_value, range_value)
        ):
            return self._target
        close = Decimal(str(close_float))
        range_decimal = Decimal(str(range_value))
        if self._target == 0:
            if close_float <= upper_value:
                return self._target
            self._target = Decimal("1")
            self._highest_close = close
            self.trailing_stop = (
                close - range_decimal * self.params.atr_stop
            )
            return self._target

        highest = max(self._highest_close or close, close)
        candidate = highest - range_decimal * self.params.atr_stop
        self._highest_close = highest
        self.trailing_stop = max(self.trailing_stop or candidate, candidate)
        if close <= self.trailing_stop:
            self._target = Decimal("0")
            self._highest_close = None
            self.trailing_stop = None
        return self._target


def _parameters(
    parameters: Mapping[str, object] | None,
) -> tuple[dict[str, object], object]:
    values = dict(parameters or {})
    tolerance = values.pop("rebalance_tolerance", Decimal("0.005"))
    return values, tolerance
