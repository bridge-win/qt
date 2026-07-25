"""Conservative deterministic OHLC bar fill simulation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from btc_backtest.engine.models import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from btc_backtest.errors import DataValidationError, ExecutionError

_BPS_DIVISOR = Decimal("10000")


class FillPolicy(BaseModel):
    """Execution costs and the declared intrabar ambiguity policy."""

    model_config = ConfigDict(frozen=True)

    fee_bps: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("0")
    intrabar_policy: Literal["adverse_first"] = "adverse_first"

    @model_validator(mode="after")
    def validate_costs(self) -> FillPolicy:
        for field, value in (
            ("fee_bps", self.fee_bps),
            ("slippage_bps", self.slippage_bps),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field} must be finite and non-negative")
        if self.slippage_bps >= _BPS_DIVISOR:
            raise ValueError("slippage_bps must be below 10000")
        return self


class BarFillModel:
    """Evaluate eligible orders against one validated OHLCV bar."""

    def __init__(self, policy: FillPolicy | None = None) -> None:
        self.policy = policy or FillPolicy()

    def evaluate(
        self,
        order: Order,
        bar: Mapping[str, object],
        timestamp: datetime,
    ) -> Fill | None:
        values = _validated_bar(bar)
        if not self._eligible(order, timestamp):
            return None
        quantity = order.remaining_quantity
        if quantity is None or quantity <= 0:
            return None

        price: Decimal | None
        reason: str
        if order.order_type is OrderType.MARKET:
            price = self._with_slippage(values["open"], order.side)
            reason = "market"
        elif order.order_type is OrderType.LIMIT:
            price = self._limit_fill(order, values)
            reason = "limit"
        elif order.order_type is OrderType.STOP:
            price = self._stop_fill(order, values)
            reason = "stop"
        else:
            price = self._stop_limit_fill(order, values)
            reason = "stop_limit"
        if price is None:
            return None
        return self._fill(order, timestamp, quantity, price, reason)

    def evaluate_bracket(
        self,
        order: Order,
        bar: Mapping[str, object],
        timestamp: datetime,
    ) -> tuple[Fill, ...]:
        values = _validated_bar(bar)
        if not self._eligible(order, timestamp):
            return ()
        if order.stop_price is None or order.take_profit_price is None:
            raise ExecutionError(
                "bracket evaluation requires stop_price and take_profit_price"
            )
        quantity = order.remaining_quantity
        if quantity is None or quantity <= 0:
            return ()

        if order.side is OrderSide.SELL:
            stop_touched = values["low"] <= order.stop_price
            target_touched = values["high"] >= order.take_profit_price
        else:
            stop_touched = values["high"] >= order.stop_price
            target_touched = values["low"] <= order.take_profit_price

        if stop_touched:
            price = self._stop_price(order.side, order.stop_price, values["open"])
            return (
                self._fill(
                    order,
                    timestamp,
                    quantity,
                    price,
                    "stop",
                ),
            )
        if target_touched:
            price = self._bounded_limit_price(
                order.side,
                order.take_profit_price,
                values["open"],
            )
            return (
                self._fill(
                    order,
                    timestamp,
                    quantity,
                    price,
                    "target",
                ),
            )
        return ()

    def _limit_fill(
        self,
        order: Order,
        bar: Mapping[str, Decimal],
    ) -> Decimal | None:
        limit = order.limit_price
        if limit is None:
            raise ExecutionError("limit order is missing limit_price")
        if order.side is OrderSide.BUY:
            if bar["low"] > limit:
                return None
        elif bar["high"] < limit:
            return None
        return self._bounded_limit_price(order.side, limit, bar["open"])

    def _stop_fill(
        self,
        order: Order,
        bar: Mapping[str, Decimal],
    ) -> Decimal | None:
        stop = order.stop_price
        if stop is None:
            raise ExecutionError("stop order is missing stop_price")
        if order.side is OrderSide.BUY:
            if bar["high"] < stop:
                return None
        elif bar["low"] > stop:
            return None
        return self._stop_price(order.side, stop, bar["open"])

    def _stop_limit_fill(
        self,
        order: Order,
        bar: Mapping[str, Decimal],
    ) -> Decimal | None:
        stop = order.stop_price
        limit = order.limit_price
        if stop is None or limit is None:
            raise ExecutionError(
                "stop-limit order requires stop_price and limit_price"
            )
        if order.side is OrderSide.BUY:
            if bar["high"] < stop or bar["low"] > limit:
                return None
        elif bar["low"] > stop or bar["high"] < limit:
            return None
        return self._bounded_limit_price(order.side, limit, limit)

    def _bounded_limit_price(
        self,
        side: OrderSide,
        limit: Decimal,
        open_price: Decimal,
    ) -> Decimal:
        if side is OrderSide.BUY:
            base = min(open_price, limit)
            return min(self._with_slippage(base, side), limit)
        base = max(open_price, limit)
        return max(self._with_slippage(base, side), limit)

    def _stop_price(
        self,
        side: OrderSide,
        stop: Decimal,
        open_price: Decimal,
    ) -> Decimal:
        base = (
            max(open_price, stop)
            if side is OrderSide.BUY
            else min(open_price, stop)
        )
        return self._with_slippage(base, side)

    def _with_slippage(self, price: Decimal, side: OrderSide) -> Decimal:
        rate = self.policy.slippage_bps / _BPS_DIVISOR
        multiplier = Decimal("1") + (
            rate if side is OrderSide.BUY else -rate
        )
        return price * multiplier

    def _fill(
        self,
        order: Order,
        timestamp: datetime,
        quantity: Decimal,
        price: Decimal,
        reason: str,
    ) -> Fill:
        fee = price * quantity * self.policy.fee_bps / _BPS_DIVISOR
        return Fill(
            id=f"{order.id}:{timestamp.isoformat()}:{reason}",
            order_id=order.id,
            order_created_at=order.created_at,
            timestamp=timestamp,
            instrument=order.instrument,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=fee,
            reason=reason,
            intrabar_policy=self.policy.intrabar_policy,
        )

    @staticmethod
    def _eligible(order: Order, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("fill timestamp must be timezone-aware")
        if timestamp < order.created_at:
            raise ExecutionError("order cannot fill before creation")
        if order.expires_at is not None and timestamp >= order.expires_at:
            return False
        return order.status in (
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        )


def _validated_bar(bar: Mapping[str, object]) -> dict[str, Decimal]:
    required = ("open", "high", "low", "close", "volume")
    missing = [field for field in required if field not in bar]
    if missing:
        raise DataValidationError(
            f"bar is missing required fields: {', '.join(missing)}"
        )
    values = {field: _decimal(bar[field], field) for field in required}
    if any(values[field] <= 0 for field in ("open", "high", "low", "close")):
        raise DataValidationError("bar prices must be positive")
    if values["volume"] < 0:
        raise DataValidationError("bar volume must be non-negative")
    if values["high"] < max(values["open"], values["close"]):
        raise DataValidationError("bar high is below open or close")
    if values["low"] > min(values["open"], values["close"]):
        raise DataValidationError("bar low is above open or close")
    if values["low"] > values["high"]:
        raise DataValidationError("bar low cannot exceed high")
    return values


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise DataValidationError(f"bar {field} must be numeric")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise DataValidationError(f"bar {field} must be numeric") from error
    if not decimal.is_finite():
        raise DataValidationError(f"bar {field} must be finite")
    return decimal
