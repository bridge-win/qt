"""Scheduled fixed and feature-scaled dollar-cost averaging strategies."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from btc_backtest.engine.models import (
    InstrumentKind,
    OrderIntent,
    OrderSide,
    OrderType,
)
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.indicators import rsi

_BPS = Decimal("10000")


class FixedDCAParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    quote_amount: Decimal = Field(default=Decimal("100"), gt=0)
    weekday: int = Field(default=0, ge=0, le=6)
    hour: int = Field(default=0, ge=0, le=23)


class SmartDCAParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_quote: Decimal = Field(default=Decimal("100"), gt=0)
    min_multiplier: Decimal = Field(default=Decimal("0.5"), gt=0)
    max_multiplier: Decimal = Field(default=Decimal("2"), ge=1)
    rsi_window: int = Field(default=14, ge=2, le=500)
    rsi_oversold: Decimal = Field(default=Decimal("30"), ge=0, le=100)
    rsi_overbought: Decimal = Field(default=Decimal("70"), ge=0, le=100)
    fear_greed_threshold: Decimal = Field(
        default=Decimal("25"),
        ge=0,
        le=100,
    )
    valuation_entry_z: Decimal = Decimal("-1")
    weekday: int = Field(default=0, ge=0, le=6)
    hour: int = Field(default=0, ge=0, le=23)

    @model_validator(mode="after")
    def validate_ranges(self) -> SmartDCAParams:
        if self.min_multiplier > 1:
            raise ValueError("min_multiplier cannot exceed one")
        if self.min_multiplier > self.max_multiplier:
            raise ValueError("min_multiplier cannot exceed max_multiplier")
        if self.rsi_oversold >= self.rsi_overbought:
            raise ValueError("rsi_oversold must be below rsi_overbought")
        return self


class _ScheduleParams(Protocol):
    weekday: int
    hour: int


class _ScheduledDCA:
    params: _ScheduleParams

    def __init__(self) -> None:
        self._last_bucket: tuple[int, int] | None = None
        self._execution_multiplier = Decimal("1")

    def initialize(self, context: InitializationContext) -> None:
        self._last_bucket = None
        fee = context.spec.fee_bps / _BPS
        slippage = context.spec.slippage_bps / _BPS
        self._execution_multiplier = (
            (Decimal("1") + fee) * (Decimal("1") + slippage)
        )

    def _scheduled(self, context: StrategyContext) -> bool:
        timestamp = context.timestamp
        if (
            timestamp.weekday() != self.params.weekday
            or timestamp.hour != self.params.hour
        ):
            return False
        iso = timestamp.isocalendar()
        bucket = (iso.year, iso.week)
        if bucket == self._last_bucket:
            return False
        self._last_bucket = bucket
        return True

    def _affordable(
        self,
        context: StrategyContext,
        quote_amount: Decimal,
    ) -> bool:
        return (
            quote_amount * self._execution_multiplier
            <= context.portfolio.cash
        )

    def finalize(self, context: FinalizationContext) -> None:
        return None


class FixedDCA(_ScheduledDCA):
    params: FixedDCAParams
    metadata = StrategyMetadata(
        id="fixed_dca",
        version="1.0.0",
        description="Buy a fixed quote amount once per scheduled UTC week.",
        warmup_bars=0,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=FixedDCAParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        super().__init__()
        self.params = FixedDCAParams.model_validate(parameters or {})

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        if not self._scheduled(context):
            return ()
        if not self._affordable(context, self.params.quote_amount):
            return ()
        return (
            OrderIntent(
                instrument=InstrumentKind.SPOT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quote_amount=self.params.quote_amount,
                reason="fixed_dca_schedule",
            ),
        )


class SmartDCA(_ScheduledDCA):
    params: SmartDCAParams
    metadata = StrategyMetadata(
        id="smart_dca",
        version="1.0.0",
        description=(
            "Scale a weekly BTC purchase using point-in-time RSI and "
            "optional valuation features."
        ),
        warmup_bars=15,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=SmartDCAParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        super().__init__()
        self.params = SmartDCAParams.model_validate(parameters or {})
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.rsi_window + 1}
        )

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        if not self._scheduled(context):
            return ()
        multiplier = self._multiplier(context)
        quote_amount = self.params.base_quote * multiplier
        if not self._affordable(context, quote_amount):
            return ()
        return (
            OrderIntent(
                instrument=InstrumentKind.SPOT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quote_amount=quote_amount,
                reason=(
                    "smart_dca_base"
                    if multiplier == 1
                    else "smart_dca_scaled"
                ),
            ),
        )

    def _multiplier(self, context: StrategyContext) -> Decimal:
        maximum = self.params.max_multiplier
        increment = (maximum - Decimal("1")) / Decimal("2")
        multiplier = Decimal("1")
        values = rsi(context.bars["close"], self.params.rsi_window)
        current_rsi = values.iloc[-1]
        if np.isfinite(current_rsi):
            rsi_value = Decimal(str(float(current_rsi)))
            if rsi_value <= self.params.rsi_oversold:
                multiplier += increment
            elif rsi_value >= self.params.rsi_overbought:
                multiplier = self.params.min_multiplier

        fear_greed = _latest_feature(context, "fear_greed")
        if (
            fear_greed is not None
            and fear_greed <= self.params.fear_greed_threshold
        ):
            multiplier += increment
        valuation = _latest_feature(context, "valuation_zscore")
        if (
            valuation is not None
            and valuation <= self.params.valuation_entry_z
        ):
            multiplier += increment
        return min(
            maximum,
            max(self.params.min_multiplier, multiplier),
        )


def _latest_feature(
    context: StrategyContext,
    column: str,
) -> Decimal | None:
    for frame in context.auxiliary.values():
        if column not in frame.columns or frame.empty:
            continue
        value = frame[column].iloc[-1]
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(numeric):
            return Decimal(str(numeric))
    return None
