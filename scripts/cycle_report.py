"""Print the current 4-year cycle position and a per-horizon action sheet.

    python scripts/cycle_report.py            # fetch live
    python scripts/cycle_report.py --local    # use data/parquet only
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd
from rich.console import Console
from rich.table import Table

from qt.core.config import load_settings
from qt.core.logging import configure_logging
from qt.data.store import ParquetStore
from qt.indicators.cycle import cycle_position


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/default.yaml")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--ohlcv-key", default="bitstamp_BTCUSD_1d")
    args = ap.parse_args()
    configure_logging("WARNING")
    settings = load_settings(args.config)
    store = ParquetStore(settings.data.parquet_dir)
    console = Console()

    if args.local:
        ohlcv = store.read("ohlcv", args.ohlcv_key)
        oc = store.read("onchain", "coinmetrics_derived")
    else:
        from qt.data.market import fetch_ohlcv
        from qt.data.onchain import fetch_coinmetrics_mvrv_z

        since = datetime.now(tz=timezone.utc) - timedelta(days=365 * 8)
        ohlcv = fetch_ohlcv("bitstamp", "BTC/USD", "1d", since=since)
        oc = fetch_coinmetrics_mvrv_z(since=since)
    if ohlcv.empty:
        raise SystemExit("no daily OHLCV")

    cp = cycle_position(ohlcv["close"],
                        mvrv_z=oc["mvrv_z"] if not oc.empty else None,
                        nupl=oc["nupl"] if not oc.empty else None)
    pos = float(cp.position.iloc[-1])
    band = cp.band.iloc[-1]
    alloc = float(cp.target_alloc.iloc[-1])

    t = Table(title=f"BTC cycle position — {pd.Timestamp(ohlcv.index[-1]).date()}")
    t.add_column("gauge")
    t.add_column("0..1", justify="right")
    for k, v in cp.components.iloc[-1].items():
        t.add_row(k, "n/a" if pd.isna(v) else f"{v:.2f}")
    t.add_row("[bold]position[/]", f"[bold]{pos:.0f}/100[/]")
    t.add_row("band", str(band))
    t.add_row("target BTC alloc", f"{alloc:.0%}")
    console.print(t)

    sheet = {
        "accumulate": ("DCA 2x base, capitulation strategy full size", "trend: buy every cross-up", "hold"),
        "hold": ("DCA base, capitulation full size", "trend: buy cross-up", "hold"),
        "neutral": ("DCA base, capitulation half size", "trend: follow crosses", "hold"),
        "trim": ("stop DCA, capitulation only on ≥4 groups", "trend: sell cross-down, no re-entry >Mayer 2.4", "sell 25% on overheat"),
        "distribute": ("no buys", "trend: exit on cross-down", "sell into overheat alerts to 10-25%"),
    }[str(band)]
    console.print(f"\n[bold]weekly[/]:  {sheet[0]}\n[bold]monthly[/]: {sheet[1]}\n[bold]cycle[/]:   {sheet[2]}")
    ev = cp.events.tail(30)
    if ev.any().any():
        console.print(f"\n[red]Pi Cycle event in last 30d:[/] {ev.any().to_dict()}")


if __name__ == "__main__":
    main()
