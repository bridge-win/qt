"""Delta-neutral spot/perpetual funding-basis carry."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import pandas as pd
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

HOURS_PER_YEAR = Decimal(24 * 365)


class FundingBasisCarryParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_apr: Decimal = Field(default=Decimal("0.15"), ge=0, le=10)
    exit_apr: Decimal = Field(default=Decimal("0.05"), ge=-10, le=10)
    weight: Decimal = Field(default=Decimal("0.40"), gt=0, le=1)
    funding_interval_hours: Decimal = Field(default=Decimal("8"), gt=0, le=168)
    negative_intervals: int = Field(default=3, ge=1, le=100)
    max_basis_pct: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)

    @model_validator(mode="after")
    def validate_thresholds(self) -> FundingBasisCarryParams:
        if self.exit_apr >= self.entry_apr:
            raise ValueError("carry exit_apr must be below entry_apr")
        return self


class FundingBasisCarry:
    metadata = StrategyMetadata(
        id="funding_basis_carry",
        version="1.0.0",
        description="Collect positive perpetual funding with paired spot exposure.",
        warmup_bars=0,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(
            InstrumentKind.SPOT,
            InstrumentKind.PERPETUAL,
        ),
        parameter_schema=FundingBasisCarryParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        self.params = FundingBasisCarryParams.model_validate(parameters or {})
        self._last_funding_timestamp: datetime | None = None
        self._negative_streak = 0

    def initialize(self, context: InitializationContext) -> None:
        self._last_funding_timestamp = None
        self._negative_streak = 0

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        if _has_pending_carry_order(context):
            return ()
        spot = context.portfolio.position(InstrumentKind.SPOT)
        perpetual = context.portfolio.position(InstrumentKind.PERPETUAL)
        flat = spot.quantity == 0 and perpetual.quantity == 0
        paired = spot.quantity > 0 and perpetual.quantity < 0
        if not flat and not paired:
            return _unpaired_exit(context, spot.quantity, perpetual.quantity)

        perpetual_price = _current_perpetual_price(context)
        funding = _latest_valid_funding(context, self.params)
        if paired and (perpetual_price is None or funding is None):
            return _paired_exit(context, spot.quantity, perpetual.quantity)
        if perpetual_price is None or funding is None:
            return ()

        funding_timestamp, funding_rate = funding
        is_new_funding = funding_timestamp != self._last_funding_timestamp
        if is_new_funding:
            self._last_funding_timestamp = funding_timestamp
            self._negative_streak = (
                self._negative_streak + 1
                if funding_rate < 0
                else 0
            )

        spot_price = _decimal(context.current_bar["close"])
        if spot_price is None or spot_price <= 0:
            return (
                _paired_exit(context, spot.quantity, perpetual.quantity)
                if paired
                else ()
            )
        basis = abs(perpetual_price / spot_price - Decimal("1"))
        annualized = (
            funding_rate
            * HOURS_PER_YEAR
            / self.params.funding_interval_hours
        )

        should_exit = (
            basis > self.params.max_basis_pct
            or annualized <= self.params.exit_apr
            or self._negative_streak >= self.params.negative_intervals
        )
        if paired:
            return (
                _paired_exit(context, spot.quantity, perpetual.quantity)
                if should_exit
                else ()
            )
        if (
            should_exit
            or annualized < self.params.entry_apr
            or not is_new_funding
        ):
            return ()
        return _entry(context, context.portfolio.equity * self.params.weight)

    def finalize(self, context: FinalizationContext) -> None:
        return None


def _entry(
    context: StrategyContext,
    quote_amount: Decimal,
) -> tuple[OrderIntent, OrderIntent]:
    group_id = f"carry:entry:{context.timestamp.isoformat()}"
    return (
        OrderIntent(
            instrument=InstrumentKind.SPOT,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quote_amount=quote_amount,
            group_id=group_id,
            atomic_group=True,
            reason="carry_entry",
        ),
        OrderIntent(
            instrument=InstrumentKind.PERPETUAL,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quote_amount=quote_amount,
            group_id=group_id,
            atomic_group=True,
            reason="carry_entry",
        ),
    )


def _paired_exit(
    context: StrategyContext,
    spot_quantity: Decimal,
    perpetual_quantity: Decimal,
) -> tuple[OrderIntent, OrderIntent]:
    group_id = f"carry:exit:{context.timestamp.isoformat()}"
    return (
        OrderIntent(
            instrument=InstrumentKind.SPOT,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            base_quantity=spot_quantity,
            group_id=group_id,
            atomic_group=True,
            reason="carry_exit",
        ),
        OrderIntent(
            instrument=InstrumentKind.PERPETUAL,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            base_quantity=abs(perpetual_quantity),
            group_id=group_id,
            atomic_group=True,
            reason="carry_exit",
        ),
    )


def _unpaired_exit(
    context: StrategyContext,
    spot_quantity: Decimal,
    perpetual_quantity: Decimal,
) -> tuple[OrderIntent, ...]:
    intents: list[OrderIntent] = []
    if spot_quantity > 0:
        intents.append(
            OrderIntent(
                instrument=InstrumentKind.SPOT,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                base_quantity=spot_quantity,
                reason="carry_unpaired_exit",
            )
        )
    if perpetual_quantity < 0:
        intents.append(
            OrderIntent(
                instrument=InstrumentKind.PERPETUAL,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                base_quantity=abs(perpetual_quantity),
                reason="carry_unpaired_exit",
            )
        )
    return tuple(intents)


def _has_pending_carry_order(context: StrategyContext) -> bool:
    return any(
        order.group_id is not None and order.group_id.startswith("carry:")
        for order in context.open_orders
    )


def _current_perpetual_price(context: StrategyContext) -> Decimal | None:
    frame = context.auxiliary.get("perpetual")
    active = pd.Timestamp(context.timestamp)
    if frame is None or frame.empty or active not in frame.index:
        return None
    return _decimal(frame.loc[active, "close"])


def _latest_valid_funding(
    context: StrategyContext,
    params: FundingBasisCarryParams,
) -> tuple[datetime, Decimal] | None:
    funding = context.auxiliary.get("funding")
    perpetual = context.auxiliary.get("perpetual")
    if funding is None or funding.empty or perpetual is None:
        return None
    rate_column = (
        "rate"
        if "rate" in funding.columns
        else "funding_rate"
        if "funding_rate" in funding.columns
        else None
    )
    if rate_column is None:
        return None
    effective = pd.Timestamp(funding.index[-1])
    if effective not in perpetual.index:
        return None
    timestamp = effective.to_pydatetime()
    if context.timestamp - timestamp > timedelta(
        hours=float(params.funding_interval_hours)
    ):
        return None
    rate = _decimal(funding.iloc[-1][rate_column])
    if rate is None:
        return None
    return timestamp, rate


def _decimal(value: object) -> Decimal | None:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None
