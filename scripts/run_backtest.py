"""Run a backtest from the local Parquet snapshot. Prints a rich report."""

from __future__ import annotations

import argparse

from rich.console import Console

from qt.backtest.engine import Backtester
from qt.core.config import load_settings
from qt.core.logging import configure_logging
from qt.data.store import ParquetStore
from qt.monitoring.reporting import format_backtest_report


def _series(store: ParquetStore, ds: str, key: str, col: str | None = None):
    d = store.read(ds, key)
    if d.empty:
        return None
    if col and col in d.columns:
        return d[col]
    return d.iloc[:, 0] if d.shape[1] == 1 else d


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/default.yaml")
    p.add_argument("--ohlcv-key", default="binance_BTCUSDT_1h")
    p.add_argument("--cash", type=float, default=100_000.0)
    args = p.parse_args()

    configure_logging("INFO")
    settings = load_settings(args.config)
    store = ParquetStore(settings.data.parquet_dir)
    ohlcv = store.read("ohlcv", args.ohlcv_key)
    if ohlcv.empty:
        raise SystemExit("No OHLCV data — run scripts/fetch_history.py first.")

    bt = Backtester(
        thresholds=settings.thresholds,
        risk_cfg=settings.risk,
        initial_cash=args.cash,
    )
    result = bt.run(
        ohlcv=ohlcv,
        funding=_series(store, "derivatives", "binance_BTCUSDT_funding", "funding_rate"),
        oi=_series(store, "derivatives", "binance_BTCUSDT_oi_1h", "oi_usd"),
        sopr=_series(store, "onchain", "glassnode_sopr_adj", "sopr_adj"),
        mvrv_z=_series(store, "onchain", "glassnode_mvrv_z", "mvrv_z"),
        nupl=_series(store, "onchain", "glassnode_nupl", "nupl"),
        puell=_series(store, "onchain", "glassnode_puell_multiple", "puell_multiple"),
        reserve_risk=_series(store, "onchain", "glassnode_reserve_risk", "reserve_risk"),
        exchange_netflow=_series(store, "onchain", "glassnode_exchange_netflow",
                                 "exchange_netflow"),
        fear_greed=_series(store, "sentiment", "fear_greed", "fear_greed"),
        social_sentiment=_series(store, "sentiment",
                                 "santiment_sentiment_weighted_total_btc",
                                 "sentiment_weighted_total_btc"),
        vix=_series(store, "macro", "fred_vix", "vix"),
        dxy=_series(store, "macro", "fred_dxy", "dxy"),
    )
    format_backtest_report(result, Console())


if __name__ == "__main__":
    main()
