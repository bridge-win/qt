# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Paper broker: live market data, simulated fills.

Reuses the backtester's SimBook so paper trading obeys the exact same
pessimistic fill rules as the backtest — paper-vs-backtest tracking error
then measures only data/latency effects, which is what Phase 3 is for.
Needs NO exchange key: fills are simulated from public candles.
"""
from __future__ import annotations

import logging

from ..models import Candle, Fill, Intent, Side
from ..backtest.fills import SimBook

log = logging.getLogger(__name__)


class PaperBroker:
    def __init__(self, maker_fee: float, taker_fee: float, slippage_bps: float):
        self.book = SimBook(maker_fee, taker_fee, slippage_bps)

    # BrokerProtocol ----------------------------------------------------------

    async def apply(self, it: Intent) -> None:
        b = self.book
        if it.action == "REPLACE_LADDER":
            b.cancel_kind(it.strategy, "LIMIT", side=it.side)
            for p, q in zip(it.prices, it.qtys):
                if q > 0:
                    b.place_limit(it.strategy, it.side, p, q)
        elif it.action == "CANCEL_LADDER":
            b.cancel_kind(it.strategy, "LIMIT", side=it.side)
        elif it.action == "CANCEL_ALL":
            b.cancel_all(it.strategy)
        elif it.action == "PLACE_TP":
            b.place_tp(it.strategy, it.side, it.price, it.qty)
        elif it.action == "PLACE_STOP":
            b.place_stop(it.strategy, it.side, it.price, it.qty)
        elif it.action in ("MARKET_EXIT", "MARKET_ENTER"):
            b.market(it.strategy, it.side, it.qty, it.reason)
        elif it.action == "LIMIT_IOC_ENTER":
            b.ioc(it.strategy, it.side, it.qty, it.price, it.reason)
        elif it.action == "CANCEL_ENTRIES":
            b.pending_market = [p for p in b.pending_market if p.strategy != it.strategy]
            b.cancel_kind(it.strategy, "LIMIT")

    async def on_candle(self, c: Candle) -> list[Fill]:
        return self.book.process_candle(c)

    async def cancel_everything(self) -> None:
        self.book.orders.clear()
        self.book.pending_market.clear()

    async def cancel_entries(self) -> None:
        self.book.pending_market.clear()
        for name in {o.strategy for o in self.book.orders.values()}:
            self.book.cancel_kind(name, "LIMIT")

    async def reconcile(self, strategies) -> None:      # nothing to reconcile on paper
        return

    async def close(self) -> None:
        return

