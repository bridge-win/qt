"""In-memory paper broker. Deterministic; uses `FillModel` for fees/slippage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from qt.backtest.fills import FillModel
from qt.core.types import OrderSide, OrderType, Trade
from qt.execution.base import Broker, Order


@dataclass
class PaperBroker(Broker):
    initial_cash: float = 100_000.0
    fills: FillModel = field(default_factory=FillModel)
    _cash: float = field(init=False)
    _positions: dict[str, float] = field(init=False, default_factory=dict)
    _avg_price: dict[str, float] = field(init=False, default_factory=dict)
    trade_log: list[Trade] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._cash = self.initial_cash

    def submit(self, order: Order, mark_price: float) -> Trade:
        if order.type != OrderType.MARKET:
            raise NotImplementedError("Paper broker currently only supports MARKET orders")
        fill_px = self.fills.fill_price(mark_price, order.side)
        notional = order.qty * fill_px
        fee = self.fills.fee(notional)
        if order.side == OrderSide.BUY:
            self._cash -= notional + fee
            prev_qty = self._positions.get(order.symbol, 0.0)
            prev_avg = self._avg_price.get(order.symbol, 0.0)
            new_qty = prev_qty + order.qty
            if new_qty > 0:
                self._avg_price[order.symbol] = (
                    (prev_avg * prev_qty + fill_px * order.qty) / new_qty
                )
            self._positions[order.symbol] = new_qty
        else:
            self._cash += notional - fee
            self._positions[order.symbol] = self._positions.get(order.symbol, 0.0) - order.qty
            if self._positions[order.symbol] <= 1e-12:
                self._positions[order.symbol] = 0.0
                self._avg_price[order.symbol] = 0.0

        trade = Trade(
            ts=datetime.now(tz=timezone.utc),
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=fill_px,
            fee=fee,
            venue="paper",
            note=order.note,
        )
        self.trade_log.append(trade)
        return trade

    def cash(self) -> float:
        return self._cash

    def position_qty(self, symbol: str) -> float:
        return self._positions.get(symbol, 0.0)

    def equity(self, mark_prices: dict[str, float]) -> float:
        eq = self._cash
        for sym, qty in self._positions.items():
            eq += qty * mark_prices.get(sym, 0.0)
        return eq
