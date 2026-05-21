"""Broker abstraction. Paper and live share this interface so strategy code
never depends on a specific venue."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

from qt.core.types import OrderSide, OrderType, Trade


@dataclass(frozen=True)
class Order:
    symbol: str
    side: OrderSide
    type: OrderType
    qty: float
    price: float | None = None
    client_id: str = ""
    note: str = ""

    def created_at(self) -> datetime:
        return datetime.now(tz=timezone.utc)


class Broker(ABC):
    """Minimal broker surface area used by the strategy.

    Implementations: PaperBroker (in-memory, deterministic) and LiveBroker
    (ccxt-backed, disabled unless `live_trading_enabled=True`).
    """

    @abstractmethod
    def submit(self, order: Order, mark_price: float) -> Trade:
        ...

    @abstractmethod
    def cash(self) -> float:
        ...

    @abstractmethod
    def position_qty(self, symbol: str) -> float:
        ...
