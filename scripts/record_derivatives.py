"""Hourly derivatives snapshot recorder.

Binance only retains ~30 days of OI / long-short history, so a 3-year
backtest of the derivatives group was really "funding only". Run this
hourly (cron / systemd timer) and the local Parquet store keeps growing:

    0 * * * *  cd /opt/qt && .venv/bin/python scripts/record_derivatives.py

Also pulls Coinglass aggregated liquidations when a key is configured.
Idempotent: upserts by timestamp.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from qt.core.config import load_settings
from qt.core.logging import configure_logging, get_logger
from qt.data.coinglass import fetch_aggregated_funding, fetch_aggregated_liquidations
from qt.data.derivatives import (
    fetch_funding_rate_history,
    fetch_long_short_ratio,
    fetch_open_interest_history,
)
from qt.data.store import ParquetStore

log = get_logger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/default.yaml")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=7, help="lookback per run (overlap is fine)")
    args = ap.parse_args()
    configure_logging("INFO")
    settings = load_settings(args.config)
    store = ParquetStore(settings.data.parquet_dir)
    since = datetime.now(tz=timezone.utc) - timedelta(days=args.days)

    fund = fetch_funding_rate_history(symbol=args.symbol, since=since)
    if not fund.empty:
        store.upsert("derivatives", f"binance_{args.symbol}_funding", fund)
    oi = fetch_open_interest_history(symbol=args.symbol, since=since)
    if not oi.empty:
        store.upsert("derivatives", f"binance_{args.symbol}_oi_1h", oi)
    lsr = fetch_long_short_ratio(symbol=args.symbol)
    if not lsr.empty:
        store.upsert("derivatives", f"binance_{args.symbol}_lsr_1h", lsr)
    log.info("recorded", funding=len(fund), oi=len(oi), lsr=len(lsr))

    if settings.coinglass_api_key:
        liq = fetch_aggregated_liquidations(settings.coinglass_api_key, since=since)
        if not liq.empty:
            store.upsert("derivatives", "coinglass_BTC_liq_1h", liq)
        agg = fetch_aggregated_funding(settings.coinglass_api_key, since=since)
        if not agg.empty:
            store.upsert("derivatives", "coinglass_BTC_funding_agg", agg)
        log.info("recorded_coinglass", liq=len(liq), funding_agg=len(agg))


if __name__ == "__main__":
    main()
