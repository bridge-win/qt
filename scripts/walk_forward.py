"""Run walk-forward analysis from the local Parquet snapshot."""

from __future__ import annotations

import argparse

from rich.console import Console
from rich.table import Table

from qt.backtest.walkforward import run_walk_forward
from qt.core.config import load_settings
from qt.core.logging import configure_logging
from qt.data.store import ParquetStore


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
    p.add_argument("--train-days", type=int, default=730)
    p.add_argument("--test-days", type=int, default=180)
    p.add_argument("--step-days", type=int, default=90)
    args = p.parse_args()

    configure_logging("INFO")
    settings = load_settings(args.config)
    store = ParquetStore(settings.data.parquet_dir)
    ohlcv = store.read("ohlcv", args.ohlcv_key)
    if ohlcv.empty:
        raise SystemExit("No OHLCV data — run scripts/fetch_history.py first.")

    aux = {
        "funding": _series(store, "derivatives", "binance_BTCUSDT_funding", "funding_rate"),
        "oi": _series(store, "derivatives", "binance_BTCUSDT_oi_1h", "oi_usd"),
        "fear_greed": _series(store, "sentiment", "fear_greed", "fear_greed"),
        "mvrv_z": _series(store, "onchain", "glassnode_mvrv_z", "mvrv_z"),
        "sopr": _series(store, "onchain", "glassnode_sopr_adj", "sopr_adj"),
        "nupl": _series(store, "onchain", "glassnode_nupl", "nupl"),
        "puell": _series(store, "onchain", "glassnode_puell_multiple", "puell_multiple"),
        "reserve_risk": _series(store, "onchain", "glassnode_reserve_risk", "reserve_risk"),
        "exchange_netflow": _series(store, "onchain", "glassnode_exchange_netflow",
                                    "exchange_netflow"),
        "vix": _series(store, "macro", "fred_vix", "vix"),
        "dxy": _series(store, "macro", "fred_dxy", "dxy"),
        "long_liquidations_usd": _series(store, "derivatives",
                                         "coinglass_BTC_liquidations", "long_liq_usd"),
    }

    result = run_walk_forward(
        ohlcv=ohlcv,
        aux_inputs=aux,
        base_thresholds=settings.thresholds,
        risk_cfg=settings.risk,
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
    )

    console = Console()
    tt = Table(title="Walk-forward per-window OOS")
    for col in ["train_start", "test_start", "trades", "ret", "sharpe", "mdd", "alloc_min_groups"]:
        tt.add_column(col)
    for w in result.windows:
        m = w.test_result.metrics if w.test_result is not None else None
        if m is None:
            tt.add_row(str(w.train_start)[:10], str(w.test_start)[:10],
                       "-", "-", "-", "-", "-")
            continue
        cfg = w.chosen_thresholds
        gmin = cfg.min_factor_groups if cfg else "-"
        tt.add_row(
            str(w.train_start)[:10], str(w.test_start)[:10],
            str(m.num_trades), f"{m.total_return:.2%}",
            f"{m.sharpe:.2f}", f"{m.max_drawdown:.2%}",
            str(gmin),
        )
    console.print(tt)

    summary = Table(title="Walk-forward aggregate OOS")
    summary.add_column("metric"); summary.add_column("value", justify="right")
    m = result.oos_metrics
    rows = [
        ("Total OOS return", f"{m.total_return:.2%}"),
        ("Sharpe (OOS combined)", f"{m.sharpe:.2f}"),
        ("Sortino (OOS combined)", f"{m.sortino:.2f}"),
        ("Calmar (OOS combined)", f"{m.calmar:.2f}"),
        ("Max DD (OOS combined)", f"{m.max_drawdown:.2%}"),
        ("Trades (OOS)", str(m.num_trades)),
        ("Win rate (OOS)", f"{m.win_rate:.1%}"),
        ("Profit factor (OOS)", f"{m.profit_factor:.2f}"),
        ("Stability ratio (σ/|μ| of test Sharpe)", f"{result.stability_ratio():.2f}"),
    ]
    for k, v in rows:
        summary.add_row(k, v)
    console.print(summary)


if __name__ == "__main__":
    main()
