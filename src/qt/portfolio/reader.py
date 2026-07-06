"""Read-only portfolio views for the dashboard and reporting.

Loads a strategy's durable ledger (state.json + trades.csv) into plain
dicts without needing the live PortfolioLedger object. Safe to call from
the dashboard thread while the runner writes.
"""

from __future__ import annotations

import contextlib
import csv
import json
from pathlib import Path
from typing import Any


def _portfolios_root(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / "portfolios"


def read_portfolio(strategy_name: str, runtime_dir: str | Path = "data/runtime") -> dict[str, Any] | None:
    """Return a snapshot dict for one strategy, or None if no ledger exists.

    Shape::

        {
          "name": "dca",
          "cash": 98000.0,
          "positions": {"BTC/USDT": 0.04},
          "avg_price": {"BTC/USDT": 50000.0},
          "realized_pnl": 120.0,
          "equity_peak": 100200.0,
          "trades": [ {ts, symbol, side, qty, price, fee, notional, cash_after, equity}, ... ],
          "num_trades": 12,
          "total_fees": 6.0,
          "last_equity": 100100.0
        }
    """

    ledger_dir = _portfolios_root(runtime_dir) / strategy_name
    state_file = ledger_dir / "state.json"
    trade_csv = ledger_dir / "trades.csv"
    if not state_file.exists():
        return None

    try:
        with open(state_file) as f:
            state = json.load(f)
    except Exception:
        return None

    trades: list[dict[str, Any]] = []
    total_fees = 0.0
    last_equity = None
    if trade_csv.exists():
        with open(trade_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Coerce numerics
                for k in ("qty", "price", "fee", "notional", "cash_after", "equity"):
                    if k in row and row[k] != "":
                        with contextlib.suppress(TypeError, ValueError):
                            row[k] = float(row[k])
                trades.append(row)
                fee_val = row.get("fee")
                if isinstance(fee_val, float):
                    total_fees += fee_val
                eq_val = row.get("equity")
                if isinstance(eq_val, float):
                    last_equity = eq_val

    return {
        "name": strategy_name,
        "cash": state.get("cash", 0.0),
        "positions": state.get("positions", {}),
        "avg_price": state.get("avg_price", {}),
        "realized_pnl": state.get("realized_pnl", 0.0),
        "equity_peak": state.get("equity_peak", 0.0),
        "trades": trades,
        "num_trades": len(trades),
        "total_fees": total_fees,
        "last_equity": last_equity if last_equity is not None else state.get("cash", 0.0),
    }


def read_all_portfolios(runtime_dir: str | Path = "data/runtime") -> list[dict[str, Any]]:
    """Return snapshot dicts for every strategy ledger under runtime_dir."""

    root = _portfolios_root(runtime_dir)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        snap = read_portfolio(child.name, runtime_dir)
        if snap is not None:
            out.append(snap)
    return out
