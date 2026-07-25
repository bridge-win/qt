"""ATR and Keltner volatility breakout strategies."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from btc_backtest.engine.models import InstrumentKind
from btc_backtest.strategies.base import StrategyContext, StrategyMetadata
from btc_backtest.strategies.indicators import atr, keltner
from btc_backtest.strategies.target_weight import TargetWeightStrategy


class ATRVolatilityBreakoutParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    atr_window: int = Field(default=14, ge=2, le=1000)
    multiplier: Decimal = Field(default=Decimal("1"), gt=0, le=20)
    exit_multiplier: Decimal = Field(default=Decimal("0"), ge=0, le=20)


class KeltnerChannelParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ema_window: int = Field(default=20, ge=2, le=1000)
    atr_window: int = Field(default=14, ge=2, le=1000)
    multiplier: Decimal = Field(default=Decimal("2"), gt=0, le=20)


class _VolatilityStrategy(TargetWeightStrategy):
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


class ATRVolatilityBreakout(_VolatilityStrategy):
    metadata = StrategyMetadata(
        id="atr_volatility_breakout",
        version="1.0.0",
        description="Break above prior close plus prior ATR.",
        warmup_bars=15,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=ATRVolatilityBreakoutParams.model_json_schema(),
    )
    entry_reason = "atr_volatility_breakout"
    exit_reason = "atr_volatility_exit"

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = ATRVolatilityBreakoutParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.atr_window + 1}
        )
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        ranges = atr(
            context.bars["high"],
            context.bars["low"],
            context.bars["close"],
            self.params.atr_window,
        )
        if len(ranges) < 2:
            return self._target
        prior_range = float(ranges.iloc[-2])
        prior_close = float(context.bars["close"].iloc[-2])
        close = float(context.current_bar["close"])
        if not all(np.isfinite(value) for value in (prior_range, prior_close, close)):
            return self._target
        if self._target == 0 and close > (
            prior_close + prior_range * float(self.params.multiplier)
        ):
            self._target = Decimal("1")
        elif self._target == 1 and close < (
            prior_close - prior_range * float(self.params.exit_multiplier)
        ):
            self._target = Decimal("0")
        return self._target


class KeltnerChannel(_VolatilityStrategy):
    metadata = StrategyMetadata(
        id="keltner_channel",
        version="1.0.0",
        description="Enter above the Keltner channel and exit its center.",
        warmup_bars=21,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=KeltnerChannelParams.model_json_schema(),
    )
    entry_reason = "keltner_breakout"
    exit_reason = "keltner_center_exit"

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = KeltnerChannelParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={
                "warmup_bars": max(
                    self.params.ema_window,
                    self.params.atr_window,
                )
                + 1
            }
        )
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        _, middle, upper = keltner(
            context.bars["high"],
            context.bars["low"],
            context.bars["close"],
            ema_window=self.params.ema_window,
            atr_window=self.params.atr_window,
            multiplier=float(self.params.multiplier),
        )
        close = float(context.current_bar["close"])
        center = float(middle.iloc[-1])
        upper_value = float(upper.iloc[-1])
        if not all(np.isfinite(value) for value in (close, center, upper_value)):
            return self._target
        if self._target == 0 and close > upper_value:
            self._target = Decimal("1")
        elif self._target == 1 and close <= center:
            self._target = Decimal("0")
        return self._target


def _parameters(
    parameters: Mapping[str, object] | None,
) -> tuple[dict[str, object], object]:
    values = dict(parameters or {})
    tolerance = values.pop("rebalance_tolerance", Decimal("0.005"))
    return values, tolerance
