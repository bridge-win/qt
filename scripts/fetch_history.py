"""One-shot data backfill.

Usage::

    python scripts/fetch_history.py --days 1095

Pulls everything available on the free tier (Binance OHLCV via ccxt,
Binance funding/OI, Coin Metrics MVRV+realized cap, alternative.me F&G).
Paid sources (Glassnode, Santiment) are pulled only if API keys are set.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from rich.console import Console

from qt.core.config import load_settings
from qt.core.logging import configure_logging, get_logger
from qt.data.derivatives import (
    fetch_funding_rate_history,
    fetch_long_short_ratio,
    fetch_open_interest_history,
)
from qt.data.macro import fetch_fred
from qt.data.market import fetch_ohlcv
from qt.data.onchain import fetch_coinmetrics, fetch_glassnode
from qt.data.sentiment import fetch_fear_greed, fetch_santiment_social
from qt.data.store import ParquetStore

console = Console()
log = get_logger(__name__)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=365 * 3)
    p.add_argument("--exchange", default="binance")
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--timeframes", nargs="+", default=["1h", "4h", "1d"])
    args = p.parse_args()

    configure_logging("INFO")
    settings = load_settings()
    store = ParquetStore(settings.data.parquet_dir)
    since = datetime.now(tz=timezone.utc) - timedelta(days=args.days)

    # OHLCV per timeframe
    for tf in args.timeframes:
        console.rule(f"OHLCV {args.exchange} {args.symbol} {tf}")
        df = fetch_ohlcv(args.exchange, args.symbol, tf, since=since)
        key = f"{args.exchange}_{args.symbol.replace('/', '')}_{tf}"
        store.upsert("ohlcv", key, df)
        console.print(f"  {len(df)} rows")

    # Derivatives
    console.rule("Derivatives")
    fund = fetch_funding_rate_history(symbol="BTCUSDT", since=since)
    store.upsert("derivatives", "binance_BTCUSDT_funding", fund)
    console.print(f"  funding: {len(fund)} rows")
    oi = fetch_open_interest_history(symbol="BTCUSDT")
    store.upsert("derivatives", "binance_BTCUSDT_oi_1h", oi)
    console.print(f"  oi: {len(oi)} rows (Binance retains ~30d)")
    lsr = fetch_long_short_ratio(symbol="BTCUSDT")
    store.upsert("derivatives", "binance_BTCUSDT_lsr_1h", lsr)
    console.print(f"  lsr: {len(lsr)} rows")

    # On-chain (free Coin Metrics)
    console.rule("On-chain (Coin Metrics community)")
    for metric in ["price_usd", "realized_cap_usd", "market_cap_usd", "mvrv",
                   "active_addr", "hashrate", "tx_volume_usd"]:
        df = fetch_coinmetrics(metric, since=since)
        store.upsert("onchain", f"coinmetrics_{metric}", df)
        console.print(f"  cm {metric}: {len(df)} rows")

    # On-chain paid (graceful no-op without key)
    if settings.glassnode_api_key:
        console.rule("On-chain (Glassnode)")
        for metric in ["mvrv_z", "sopr_adj", "lth_sopr", "nupl",
                       "exchange_netflow", "puell_multiple", "reserve_risk"]:
            df = fetch_glassnode(metric, api_key=settings.glassnode_api_key, since=since)
            store.upsert("onchain", f"glassnode_{metric}", df)
            console.print(f"  gn {metric}: {len(df)} rows")

    # Sentiment
    console.rule("Sentiment")
    fg = fetch_fear_greed(limit=0)
    store.upsert("sentiment", "fear_greed", fg)
    console.print(f"  fear_greed: {len(fg)} rows")
    if settings.santiment_api_key:
        for metric in ["sentiment_weighted_total_btc", "social_volume_total"]:
            df = fetch_santiment_social(settings.santiment_api_key, metric, since=since)
            store.upsert("sentiment", f"santiment_{metric}", df)
            console.print(f"  san {metric}: {len(df)} rows")

    # Macro
    if settings.fred_api_key:
        console.rule("Macro (FRED)")
        for metric in ["us10y", "fed_funds", "m2", "cpi", "dxy", "vix"]:
            df = fetch_fred(metric, api_key=settings.fred_api_key, since=since)
            store.upsert("macro", f"fred_{metric}", df)
            console.print(f"  fred {metric}: {len(df)} rows")

    console.rule("[green]done[/]")


if __name__ == "__main__":
    main()
