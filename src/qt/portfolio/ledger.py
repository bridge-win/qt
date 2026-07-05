"""Durable portfolio ledger for paper trading — trades, P&L, equity curve."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from qt.core.logging import get_logger
from qt.core.types import OrderSide, Trade

log = get_logger(__name__)


@dataclass
class PortfolioSnapshot:
    """Current portfolio state snapshot."""

    ts: datetime
    cash: float
    positions: dict[str, float]  # symbol -> qty
    avg_price: dict[str, float]  # symbol -> entry price
    equity: float
    realized_pnl: float
    unrealized_pnl: float = 0.0


@dataclass
class _TradeRecord:
    """Single trade written to CSV ledger."""

    ts: str  # ISO format
    symbol: str
    side: str  # BUY or SELL
    qty: float
    price: float
    fee: float
    notional: float
    cash_after: float
    equity: float


class PortfolioLedger:
    """Durable ledger: keeps JSON snapshot of current state + append-only CSV trade log."""

    def __init__(self, strategy_name: str, runtime_dir: Path | str = "data/runtime"):
        self.strategy_name = strategy_name
        self.runtime_dir = Path(runtime_dir)
        self.ledger_dir = self.runtime_dir / "portfolios" / strategy_name
        self.ledger_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = self.ledger_dir / "state.json"
        self.trade_csv = self.ledger_dir / "trades.csv"

        # In-memory state
        self.cash = 0.0
        self.positions: dict[str, float] = {}
        self.avg_price: dict[str, float] = {}
        self.realized_pnl = 0.0
        self.equity_peak = 0.0

        self._ensure_trade_csv()
        self._load_or_init()

    def _ensure_trade_csv(self) -> None:
        """Create CSV header if file doesn't exist."""
        if not self.trade_csv.exists():
            with open(self.trade_csv, "w", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "ts", "symbol", "side", "qty", "price", "fee",
                        "notional", "cash_after", "equity",
                    ],
                )
                writer.writeheader()

    def _load_or_init(self) -> None:
        """Load prior state or start fresh."""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    data = json.load(f)
                    self.cash = data.get("cash", 0.0)
                    self.positions = data.get("positions", {})
                    self.avg_price = data.get("avg_price", {})
                    self.realized_pnl = data.get("realized_pnl", 0.0)
                    self.equity_peak = data.get("equity_peak", 0.0)
                log.info("portfolio_loaded", strategy=self.strategy_name, cash=self.cash)
            except Exception as e:
                log.warning("portfolio_load_failed", strategy=self.strategy_name, error=str(e))
                self._reset()
        else:
            self._reset()

    def _reset(self) -> None:
        """Initialize with no state (caller will set initial_cash)."""
        self.cash = 0.0
        self.positions = {}
        self.avg_price = {}
        self.realized_pnl = 0.0
        self.equity_peak = 0.0

    def set_initial_cash(self, initial: float) -> None:
        """Set starting cash (if not yet initialized)."""
        if self.cash == 0.0 and self.equity_peak == 0.0:
            self.cash = initial
            self.equity_peak = initial
            self._save()

    def record_trade(
        self,
        trade: Trade,
        mark_prices: dict[str, float],
        total_initial_cash: float,
    ) -> PortfolioSnapshot:
        """Record a trade, update state, append to CSV, return snapshot."""

        symbol = trade.symbol
        if trade.side == OrderSide.BUY:
            # Cost basis includes the trade fee
            self.cash -= trade.qty * trade.price + trade.fee
            prev_qty = self.positions.get(symbol, 0.0)
            prev_avg = self.avg_price.get(symbol, 0.0)
            new_qty = prev_qty + trade.qty
            # Cost per unit includes fees proportionally
            cost_with_fee = (trade.qty * trade.price + trade.fee) / trade.qty
            if new_qty > 0:
                self.avg_price[symbol] = (prev_avg * prev_qty + cost_with_fee * trade.qty) / new_qty
            self.positions[symbol] = new_qty
        else:  # SELL
            self.cash += trade.qty * trade.price - trade.fee
            prev_qty = self.positions.get(symbol, 0.0)
            prev_avg = self.avg_price.get(symbol, 0.0)
            qty_sold = trade.qty
            # realized_pnl = proceeds - cost_basis - sell_fee
            # prev_avg already includes buy fees, so just subtract it
            realized = (trade.price * qty_sold - trade.fee) - (prev_avg * qty_sold)
            self.realized_pnl += realized
            self.positions[symbol] = prev_qty - qty_sold
            if self.positions[symbol] <= 1e-12:
                self.positions[symbol] = 0.0
                self.avg_price[symbol] = 0.0

        equity = self._compute_equity(mark_prices)
        if equity > self.equity_peak:
            self.equity_peak = equity

        # Append trade to CSV
        notional = trade.qty * trade.price
        record = _TradeRecord(
            ts=trade.ts.isoformat(),
            symbol=symbol,
            side=trade.side.value,
            qty=trade.qty,
            price=trade.price,
            fee=trade.fee,
            notional=notional,
            cash_after=self.cash,
            equity=equity,
        )
        with open(self.trade_csv, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=asdict(record).keys())
            writer.writerow(asdict(record))

        self._save()
        return self.snapshot(mark_prices)

    def _compute_equity(self, mark_prices: dict[str, float]) -> float:
        """Equity = cash + sum(position qty * mark price)."""
        eq = self.cash
        for sym, qty in self.positions.items():
            eq += qty * mark_prices.get(sym, 0.0)
        return eq

    def snapshot(self, mark_prices: dict[str, float]) -> PortfolioSnapshot:
        """Current portfolio state."""
        equity = self._compute_equity(mark_prices)
        unrealized = sum(
            self.positions.get(sym, 0.0) * (mark_prices.get(sym, 0.0) - self.avg_price.get(sym, 0.0))
            for sym in self.positions
        )
        return PortfolioSnapshot(
            ts=datetime.now(tz=timezone.utc),
            cash=self.cash,
            positions=dict(self.positions),
            avg_price=dict(self.avg_price),
            equity=equity,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
        )

    def _save(self) -> None:
        """Persist current state to JSON."""
        data = {
            "cash": self.cash,
            "positions": {k: v for k, v in self.positions.items() if v > 1e-12},
            "avg_price": {k: v for k, v in self.avg_price.items() if k in self.positions and self.positions[k] > 1e-12},
            "realized_pnl": self.realized_pnl,
            "equity_peak": self.equity_peak,
        }
        with open(self.state_file, "w") as f:
            json.dump(data, f, indent=2)
