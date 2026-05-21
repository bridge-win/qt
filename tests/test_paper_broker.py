"""PaperBroker accounting tests."""

from __future__ import annotations

from qt.backtest.fills import FillModel
from qt.core.types import OrderSide, OrderType
from qt.execution.base import Order
from qt.execution.paper import PaperBroker


def test_buy_then_sell_realizes_pnl() -> None:
    broker = PaperBroker(initial_cash=10_000, fills=FillModel(fee_bps=0, slippage_bps=0))
    o1 = Order(symbol="BTC/USDT", side=OrderSide.BUY, type=OrderType.MARKET, qty=0.1)
    broker.submit(o1, mark_price=40_000)
    assert broker.cash() == 10_000 - 4_000
    assert broker.position_qty("BTC/USDT") == 0.1

    o2 = Order(symbol="BTC/USDT", side=OrderSide.SELL, type=OrderType.MARKET, qty=0.1)
    broker.submit(o2, mark_price=44_000)
    assert broker.cash() == 10_000 - 4_000 + 4_400
    assert broker.position_qty("BTC/USDT") == 0.0


def test_fees_and_slippage_reduce_pnl() -> None:
    broker = PaperBroker(initial_cash=10_000, fills=FillModel(fee_bps=10, slippage_bps=20))
    broker.submit(Order(symbol="BTC/USDT", side=OrderSide.BUY,
                        type=OrderType.MARKET, qty=0.1), mark_price=40_000)
    broker.submit(Order(symbol="BTC/USDT", side=OrderSide.SELL,
                        type=OrderType.MARKET, qty=0.1), mark_price=40_000)
    # Round-trip with no price change should be a small loss.
    assert broker.cash() < 10_000
