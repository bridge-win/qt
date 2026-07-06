"""Monthly honesty report: every strategy vs plain DCA + live P&L digest.

    python scripts/monthly_report.py [--synthetic]

Two questions, answered automatically:

1. Backtest: does each strategy beat plain weekly DCA over history?
   (Ground rule #3 — if not, it should be turned off.)
2. Live/paper: what did each strategy's ledger actually do this period?

Writes ``data/reports/monthly_<ts>.json`` and prints a table. Intended to
run on a cron once a month; also runnable by hand any time.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from qt.backtest.strategy_backtest import run_strategy_backtest, synthetic_btc_ohlcv
from qt.core.config import load_settings
from qt.data.store import ParquetStore
from qt.portfolio import read_all_portfolios
from qt.reporting import compare_to_dca_benchmark

console = Console()
STRATS = ("dca", "trend", "carry")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--synthetic", action="store_true", help="Use synthetic data")
    p.add_argument("--config", default="config/default.yaml")
    p.add_argument("--runtime-dir", default="data/runtime")
    p.add_argument("--output-dir", default="data/reports")
    p.add_argument("--ohlcv-key", default="binance_BTCUSDT_1h")
    args = p.parse_args()

    settings = load_settings(args.config)

    ohlcv = None
    if not args.synthetic:
        store = ParquetStore(settings.data.parquet_dir)
        df = store.read("ohlcv", args.ohlcv_key)
        ohlcv = df if not df.empty else None

    report: dict[str, object] = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "benchmark": [],
        "live": [],
    }

    # 1) Backtest vs DCA
    bt_table = Table(title="Backtest: strategy vs plain DCA")
    for c in ("Strategy", "Final", "DCA", "Beats?", "Verdict"):
        bt_table.add_column(c)
    for strat in STRATS:
        outcome = run_strategy_backtest(strat, ohlcv, allow_synthetic=True)
        used = ohlcv if ohlcv is not None and not outcome.synthetic else synthetic_btc_ohlcv()
        cmp = compare_to_dca_benchmark(strat, outcome.equity, used)
        report["benchmark"].append(cmp.as_dict())  # type: ignore[attr-defined]
        mark = "[green]YES[/]" if cmp.beats_benchmark else "[red]no[/]"
        bt_table.add_row(
            strat, f"{cmp.strategy_final:,.0f}", f"{cmp.benchmark_final:,.0f}",
            mark, cmp.verdict[:60],
        )
    console.print(bt_table)

    # 2) Live/paper ledger digest
    portfolios = read_all_portfolios(args.runtime_dir)
    if portfolios:
        live_table = Table(title="Live/paper ledger this period")
        for c in ("Strategy", "Equity", "Realized P&L", "Trades", "Fees"):
            live_table.add_column(c)
        for pf in portfolios:
            report["live"].append({  # type: ignore[attr-defined]
                "name": pf["name"], "equity": pf["last_equity"],
                "realized_pnl": pf["realized_pnl"], "num_trades": pf["num_trades"],
                "total_fees": pf["total_fees"],
            })
            live_table.add_row(
                str(pf["name"]), f"{pf['last_equity']:,.2f}",
                f"{pf['realized_pnl']:,.2f}", str(pf["num_trades"]),
                f"{pf['total_fees']:,.2f}",
            )
        console.print(live_table)
    else:
        console.print("[yellow]no live/paper ledgers yet — run the strategies first[/]")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"monthly_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str))
    (out_dir / "latest.json").write_text(json.dumps(report, indent=2, default=str))
    console.print(f"[green]report[/] {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
