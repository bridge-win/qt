"""Portfolio ledger and paper execution tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from qt.core.types import OrderSide, Trade
from qt.portfolio import PortfolioLedger


def test_portfolio_ledger_initialization(tmp_path: Path) -> None:
    ledger = PortfolioLedger("test_strategy", tmp_path)
    assert ledger.cash == 0.0
    assert ledger.positions == {}

    ledger.set_initial_cash(100_000.0)
    assert ledger.cash == 100_000.0


def test_portfolio_buy_and_sell(tmp_path: Path) -> None:
    ledger = PortfolioLedger("test_strategy", tmp_path)
    ledger.set_initial_cash(100_000.0)

    # Buy BTC at 50000 with 50 fee -> cost basis 50050
    buy_trade = Trade(
        ts=datetime.now(tz=timezone.utc),
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        qty=1.0,
        price=50_000.0,
        fee=50.0,
    )
    snap = ledger.record_trade(buy_trade, {"BTC/USDT": 50_000.0}, 100_000.0)
    assert snap.cash == 49_950.0
    assert snap.positions["BTC/USDT"] == 1.0
    assert snap.avg_price["BTC/USDT"] == 50_050.0  # price + fee/qty

    # Mark price up to 51000
    snap = ledger.snapshot({"BTC/USDT": 51_000.0})
    assert snap.equity == 49_950.0 + 51_000.0  # cash + position value
    # unrealized = (51000 - 50050) * 1 = 950 (includes fee in cost basis)
    assert snap.unrealized_pnl == 950.0

    # Sell at 51000 with 51 fee
    sell_trade = Trade(
        ts=datetime.now(tz=timezone.utc),
        symbol="BTC/USDT",
        side=OrderSide.SELL,
        qty=1.0,
        price=51_000.0,
        fee=51.0,
    )
    snap = ledger.record_trade(sell_trade, {"BTC/USDT": 51_000.0}, 100_000.0)
    assert snap.cash == 49_950.0 + 51_000.0 - 51.0
    # realized_pnl = (51000*1 - 51) - (50050*1) = 50949 - 50050 = 899
    assert snap.realized_pnl == 899.0


def test_portfolio_persistence(tmp_path: Path) -> None:
    """Test that portfolio state is saved and loaded."""
    ledger1 = PortfolioLedger("persist_test", tmp_path)
    ledger1.set_initial_cash(100_000.0)

    trade = Trade(
        ts=datetime.now(tz=timezone.utc),
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        qty=0.5,
        price=50_000.0,
        fee=25.0,
    )
    ledger1.record_trade(trade, {"BTC/USDT": 50_000.0}, 100_000.0)

    # Load from disk
    ledger2 = PortfolioLedger("persist_test", tmp_path)
    assert ledger2.cash == 74_975.0
    assert ledger2.positions["BTC/USDT"] == 0.5


def test_portfolio_csv_log(tmp_path: Path) -> None:
    """Test that trades are logged to CSV."""
    ledger = PortfolioLedger("csv_test", tmp_path)
    ledger.set_initial_cash(100_000.0)

    trade = Trade(
        ts=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        qty=1.0,
        price=50_000.0,
        fee=50.0,
    )
    ledger.record_trade(trade, {"BTC/USDT": 50_000.0}, 100_000.0)

    trade_csv = tmp_path / "portfolios" / "csv_test" / "trades.csv"
    assert trade_csv.exists()
    with open(trade_csv) as f:
        lines = f.readlines()
    assert len(lines) == 2  # header + 1 trade
    assert "BTC/USDT" in lines[1]
    assert "50000" in lines[1]
