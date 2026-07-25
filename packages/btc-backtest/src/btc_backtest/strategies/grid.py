"""Bounded, idempotent spot inventory grid."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

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


class GridParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lower: Decimal = Field(default=Decimal("80000"), gt=0)
    upper: Decimal = Field(default=Decimal("120000"), gt=0)
    levels: int = Field(default=9, ge=2, le=100)
    quote_per_level: Decimal = Field(default=Decimal("100"), gt=0)
    max_inventory_weight: Decimal = Field(default=Decimal("1"), gt=0, le=1)

    @model_validator(mode="after")
    def validate_range(self) -> GridParams:
        if self.lower >= self.upper:
            raise ValueError("grid lower must be below upper")
        return self


class GridRebalance:
    metadata = StrategyMetadata(
        id="grid_rebalance",
        version="1.0.0",
        description="Maintain a bounded, cash- and inventory-capped spot grid.",
        warmup_bars=0,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=GridParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        self.params = GridParams.model_validate(parameters or {})

    def initialize(self, context: InitializationContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        close = Decimal(str(context.current_bar["close"]))
        if close <= 0:
            return ()
        existing = {order.group_id for order in context.open_orders if order.group_id is not None}
        levels = _levels(self.params)
        position = context.portfolio.position(InstrumentKind.SPOT)
        inventory_value = position.quantity * close
        maximum_inventory = context.portfolio.equity * self.params.max_inventory_weight
        remaining_buy = min(
            context.portfolio.cash,
            max(Decimal("0"), maximum_inventory - inventory_value),
        )
        remaining_sell = position.quantity
        intents: list[OrderIntent] = []
        for level in levels:
            if level == close:
                continue
            group_id = f"grid:{_level_text(level)}"
            if group_id in existing:
                continue
            if level < close:
                quote = min(self.params.quote_per_level, remaining_buy)
                if quote <= 0:
                    continue
                intents.append(
                    OrderIntent(
                        instrument=InstrumentKind.SPOT,
                        side=OrderSide.BUY,
                        order_type=OrderType.LIMIT,
                        quote_amount=quote,
                        limit_price=level,
                        group_id=group_id,
                        reason="grid_buy_level",
                    )
                )
                remaining_buy -= quote
                continue
            base = min(self.params.quote_per_level / level, remaining_sell)
            if base <= 0:
                continue
            intents.append(
                OrderIntent(
                    instrument=InstrumentKind.SPOT,
                    side=OrderSide.SELL,
                    order_type=OrderType.LIMIT,
                    base_quantity=base,
                    limit_price=level,
                    group_id=group_id,
                    reason="grid_sell_level",
                )
            )
            remaining_sell -= base
        return tuple(intents)

    def finalize(self, context: FinalizationContext) -> None:
        return None


def _levels(params: GridParams) -> tuple[Decimal, ...]:
    step = (params.upper - params.lower) / Decimal(params.levels - 1)
    return tuple(params.lower + step * index for index in range(params.levels))


def _level_text(level: Decimal) -> str:
    return format(level.normalize(), "f")
