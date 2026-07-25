"""Immutable execution, portfolio, and backtest result contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from btc_backtest.data.models import DataManifest, DataRequest


class InstrumentKind(str, Enum):
    SPOT = "spot"
    PERPETUAL = "perpetual"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


def _require_finite(value: Decimal, field: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


def _require_positive(value: Decimal, field: str) -> Decimal:
    _require_finite(value, field)
    if value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _require_non_negative(value: Decimal, field: str) -> Decimal:
    _require_finite(value, field)
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value


class OrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    instrument: InstrumentKind = InstrumentKind.SPOT
    side: OrderSide
    order_type: OrderType
    quote_amount: Decimal | None = None
    base_quantity: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    group_id: str | None = Field(default=None, min_length=1)
    atomic_group: bool = False
    reason: str = Field(min_length=1)
    signal_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_intent(self) -> OrderIntent:
        sizes = (self.quote_amount is not None, self.base_quantity is not None)
        if sum(sizes) != 1:
            raise ValueError(
                "exactly one of quote_amount or base_quantity must be provided"
            )
        if self.quote_amount is not None:
            _require_positive(self.quote_amount, "quote_amount")
        if self.base_quantity is not None:
            _require_positive(self.base_quantity, "base_quantity")
        if self.limit_price is not None:
            _require_positive(self.limit_price, "limit_price")
        if self.stop_price is not None:
            _require_positive(self.stop_price, "stop_price")
        if self.take_profit_price is not None:
            _require_positive(self.take_profit_price, "take_profit_price")
        if (
            self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
            and self.limit_price is None
        ):
            raise ValueError("limit_price is required for limit orders")
        if (
            self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
            and self.stop_price is None
        ):
            raise ValueError("stop_price is required for stop orders")
        if self.atomic_group and self.group_id is None:
            raise ValueError("group_id is required for an atomic_group")
        return self


class Order(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    created_at: datetime
    instrument: InstrumentKind = InstrumentKind.SPOT
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    remaining_quantity: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    status: OrderStatus = OrderStatus.OPEN
    expires_at: datetime | None = None
    group_id: str | None = Field(default=None, min_length=1)
    atomic_group: bool = False
    reason: str = Field(min_length=1)
    signal_ids: tuple[str, ...] = ()

    @field_validator("created_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_order(self) -> Order:
        _require_positive(self.quantity, "quantity")
        if self.remaining_quantity is None:
            object.__setattr__(self, "remaining_quantity", self.quantity)
        else:
            _require_non_negative(self.remaining_quantity, "remaining_quantity")
            if self.remaining_quantity > self.quantity:
                raise ValueError("remaining_quantity cannot exceed quantity")
        if self.limit_price is not None:
            _require_positive(self.limit_price, "limit_price")
        if self.stop_price is not None:
            _require_positive(self.stop_price, "stop_price")
        if self.take_profit_price is not None:
            _require_positive(self.take_profit_price, "take_profit_price")
        if (
            self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
            and self.limit_price is None
        ):
            raise ValueError("limit_price is required for limit orders")
        if (
            self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
            and self.stop_price is None
        ):
            raise ValueError("stop_price is required for stop orders")
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("expires_at cannot precede order creation")
        if self.atomic_group and self.group_id is None:
            raise ValueError("group_id is required for an atomic_group")
        return self


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    order_created_at: datetime
    timestamp: datetime
    instrument: InstrumentKind
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    reason: str = Field(min_length=1)
    intrabar_policy: Literal["adverse_first"] = "adverse_first"

    @field_validator("order_created_at", "timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_fill(self) -> Fill:
        _require_positive(self.quantity, "quantity")
        _require_positive(self.price, "price")
        _require_non_negative(self.fee, "fee")
        if self.timestamp < self.order_created_at:
            raise ValueError("fill timestamp cannot precede order creation")
        return self

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


class FundingEvent(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    timestamp: datetime
    instrument: Literal[InstrumentKind.PERPETUAL] = InstrumentKind.PERPETUAL
    amount: Decimal
    rate: Decimal

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_funding(self) -> FundingEvent:
        _require_finite(self.amount, "amount")
        _require_finite(self.rate, "rate")
        return self


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument: InstrumentKind
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    funding_pnl: Decimal = Decimal("0")

    @model_validator(mode="after")
    def validate_position(self) -> Position:
        for field, value in (
            ("quantity", self.quantity),
            ("average_price", self.average_price),
            ("realized_pnl", self.realized_pnl),
            ("fees_paid", self.fees_paid),
            ("funding_pnl", self.funding_pnl),
        ):
            _require_finite(value, field)
        if self.instrument is InstrumentKind.SPOT and self.quantity < 0:
            raise ValueError("spot quantity cannot be negative")
        if self.average_price < 0:
            raise ValueError("average_price must be non-negative")
        if self.fees_paid < 0:
            raise ValueError("fees_paid must be non-negative")
        return self


class PortfolioSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    positions: tuple[Position, ...]

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_values(self) -> PortfolioSnapshot:
        for field, value in (
            ("cash", self.cash),
            ("equity", self.equity),
            ("realized_pnl", self.realized_pnl),
            ("unrealized_pnl", self.unrealized_pnl),
        ):
            _require_finite(value, field)
        if self.cash < 0:
            raise ValueError("cash cannot be negative")
        return self

    def position(self, instrument: InstrumentKind | str) -> Position:
        target = InstrumentKind(instrument)
        for position in self.positions:
            if position.instrument is target:
                return position
        raise KeyError(f"snapshot has no {target.value} position")


class Trade(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    instrument: InstrumentKind
    opened_at: datetime
    closed_at: datetime
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    realized_pnl: Decimal
    fees: Decimal

    @field_validator("opened_at", "closed_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_trade(self) -> Trade:
        if self.closed_at < self.opened_at:
            raise ValueError("trade close cannot precede open")
        _require_positive(self.quantity, "quantity")
        _require_positive(self.entry_price, "entry_price")
        _require_positive(self.exit_price, "exit_price")
        _require_finite(self.realized_pnl, "realized_pnl")
        _require_non_negative(self.fees, "fees")
        return self


class BacktestSpec(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    strategy: str = Field(min_length=1)
    strategy_params: Mapping[str, object] = Field(
        default_factory=dict,
        validate_default=True,
    )
    data: DataRequest
    auxiliary_data: tuple[DataRequest, ...] = ()
    initial_cash: Decimal = Decimal("10000")
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("5")
    intrabar_policy: Literal["adverse_first"] = "adverse_first"
    seed: int = 7

    @field_validator("strategy_params")
    @classmethod
    def freeze_parameters(
        cls,
        value: Mapping[str, object],
    ) -> Mapping[str, object]:
        return MappingProxyType(dict(value))

    @field_serializer("strategy_params")
    def serialize_parameters(
        self,
        value: Mapping[str, object],
    ) -> dict[str, object]:
        return dict(value)

    @model_validator(mode="after")
    def validate_costs(self) -> BacktestSpec:
        _require_positive(self.initial_cash, "initial_cash")
        _require_non_negative(self.fee_bps, "fee_bps")
        _require_non_negative(self.slippage_bps, "slippage_bps")
        return self


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    run_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    data_manifests: tuple[DataManifest, ...]
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    positions: tuple[Position, ...] = ()
    snapshots: tuple[PortfolioSnapshot, ...]
    trades: tuple[Trade, ...]
    signal_ids: tuple[str, ...] = ()
    diagnostics: Mapping[str, object] = Field(
        default_factory=dict,
        validate_default=True,
    )
    warnings: tuple[str, ...] = ()

    @field_validator("diagnostics")
    @classmethod
    def freeze_diagnostics(
        cls,
        value: Mapping[str, object],
    ) -> Mapping[str, object]:
        return MappingProxyType(dict(value))

    @field_serializer("diagnostics")
    def serialize_diagnostics(
        self,
        value: Mapping[str, object],
    ) -> dict[str, object]:
        return dict(value)
