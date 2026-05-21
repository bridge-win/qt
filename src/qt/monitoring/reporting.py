"""Backtest report formatting (rich console output)."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from qt.backtest.engine import BacktestResult


def format_backtest_report(result: BacktestResult, console: Console | None = None) -> None:
    console = console or Console()
    m = result.metrics
    table = Table(title="Backtest summary", show_lines=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta", justify="right")
    rows = [
        ("Total return", f"{m.total_return:.2%}"),
        ("CAGR", f"{m.cagr:.2%}"),
        ("Sharpe", f"{m.sharpe:.2f}"),
        ("Sortino", f"{m.sortino:.2f}"),
        ("Calmar", f"{m.calmar:.2f}"),
        ("Max drawdown", f"{m.max_drawdown:.2%}"),
        ("Trades", str(m.num_trades)),
        ("Win rate", f"{m.win_rate:.1%}"),
        ("Avg win", f"{m.avg_win:.2f}"),
        ("Avg loss", f"{m.avg_loss:.2f}"),
        ("Profit factor", f"{m.profit_factor:.2f}"),
        ("Avg holding (bars)", f"{m.avg_holding_bars:.1f}"),
    ]
    for k, v in rows:
        table.add_row(k, v)
    console.print(table)

    if not result.trades.empty:
        tt = Table(title="Last 10 trades")
        for col in ["entry_ts", "exit_ts", "entry_price", "exit_price", "qty", "pnl", "reason"]:
            tt.add_column(col)
        for _, r in result.trades.tail(10).iterrows():
            tt.add_row(
                str(r["entry_ts"]),
                str(r["exit_ts"]),
                f"{r['entry_price']:.2f}",
                f"{r['exit_price']:.2f}",
                f"{r['qty']:.4f}",
                f"{r['pnl']:.2f}",
                str(r.get("reason", "")),
            )
        console.print(tt)
