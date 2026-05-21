"""Core domain types shared across data, signals, risk, and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class SignalKind(str, Enum):
    ENTRY_LONG = "entry_long"
    EXIT = "exit"
    REDUCE = "reduce"
    HOLD = "hold"


@dataclass(frozen=True)
class Candle:
    """OHLCV candle. Timestamps are UTC, inclusive of open, exclusive of close."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            object.__setattr__(self, "ts", self.ts.replace(tzinfo=timezone.utc))


# Alias for callers who want a more generic name.
Bar = Candle


@dataclass(frozen=True)
class Signal:
    """A trading signal emitted by the signal engine."""

    ts: datetime
    kind: SignalKind
    score: float
    """Signal strength in [0, 1]. Below entry threshold -> no trade."""
    reasons: tuple[str, ...] = field(default_factory=tuple)
    """Human-readable list of factor conditions that fired."""
    factors: dict[str, float] = field(default_factory=dict)
    """Per-factor numeric contributions for audit / explainability."""
    target_quote_alloc: float = 0.0
    """Suggested fraction of total equity to deploy if entry fires (0..1)."""


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    opened_ts: datetime | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None
    time_stop_ts: datetime | None = None

    @property
    def is_flat(self) -> bool:
        return self.qty == 0.0

    def notional(self, mark_price: float) -> float:
        return self.qty * mark_price

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.is_flat:
            return 0.0
        return (mark_price - self.avg_price) * self.qty


@dataclass(frozen=True)
class Trade:
    ts: datetime
    symbol: str
    side: OrderSide
    qty: float
    price: float
    fee: float = 0.0
    fee_ccy: str = "USDT"
    venue: Literal["paper", "binance", "okx", "bybit", "coinbase"] = "paper"
    note: str = ""

    @property
    def notional(self) -> float:
        return self.qty * self.price
