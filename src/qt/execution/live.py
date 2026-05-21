"""Live broker (ccxt). DISABLED by default — requires Settings.live_trading_enabled.

This file is intentionally minimal; it documents the interface. Wire up
order submission, partial fills, and error handling on a per-venue basis
once the strategy has passed full paper-trading validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from qt.core.logging import get_logger
from qt.core.types import OrderSide, OrderType, Trade
from qt.execution.base import Broker, Order

log = get_logger(__name__)


class LiveTradingDisabled(RuntimeError):
    """Raised when a live order would be submitted but the kill-switch is on."""


@dataclass
class LiveBroker(Broker):
    """ccxt-backed live broker. Requires settings.live_trading_enabled."""

    venue: str = "binance"
    enabled: bool = False
    _ccxt_client: object | None = None  # ccxt.Exchange

    def __post_init__(self) -> None:
        if not self.enabled:
            log.warning("live_broker_disabled", venue=self.venue,
                        hint="set QT_LIVE_TRADING_ENABLED=true to enable")

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise LiveTradingDisabled(
                "Live trading is disabled. Set QT_LIVE_TRADING_ENABLED=true after "
                "completing paper-trading validation and risk review."
            )

    def submit(self, order: Order, mark_price: float) -> Trade:
        self._require_enabled()
        # Implement per-venue submission via ccxt here.
        raise NotImplementedError(
            "LiveBroker.submit must be wired to a specific ccxt exchange. "
            "See docs/architecture.md for the integration checklist."
        )

    def cash(self) -> float:
        self._require_enabled()
        raise NotImplementedError

    def position_qty(self, symbol: str) -> float:
        self._require_enabled()
        raise NotImplementedError

    @staticmethod
    def _ts_now() -> datetime:
        return datetime.now(tz=timezone.utc)
