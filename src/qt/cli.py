"""Typer CLI for QT: fetch data, run backtests, paper trade.

Usage examples (after `pip install -e .`)::

    qt fetch-ohlcv --exchange binance --symbol BTC/USDT --timeframe 1h --days 365
    qt fetch-onchain --metric mvrv
    qt backtest --config config/default.yaml
    qt paper run --duration 1h
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer
from rich.console import Console

from qt.core.config import load_settings
from qt.core.logging import configure_logging, get_logger

app = typer.Typer(help="QT — BTC quantitative trading platform")
data_app = typer.Typer(help="Data ingestion commands")
app.add_typer(data_app, name="data")

log = get_logger(__name__)
console = Console()


@app.callback()
def _bootstrap(
    config: Path | None = typer.Option(None, help="Path to YAML config file"),
    log_level: str = typer.Option("INFO", help="Log level"),
    ctx: typer.Context = None,
) -> None:
    configure_logging(level=log_level)
    settings = load_settings(config) if config else load_settings()
    if ctx is not None:
        ctx.obj = settings


@data_app.command("fetch-ohlcv")
def fetch_ohlcv_cmd(
    exchange: str = "binance",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    days: int = 365,
    ctx: typer.Context = None,
) -> None:
    """Fetch and persist OHLCV history to the local Parquet store."""

    from qt.data.market import fetch_ohlcv
    from qt.data.store import ParquetStore

    settings = ctx.obj
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    df = fetch_ohlcv(exchange, symbol, timeframe, since=since)
    key = f"{exchange}_{symbol.replace('/', '')}_{timeframe}"
    store = ParquetStore(settings.data.parquet_dir)
    p = store.upsert("ohlcv", key, df)
    console.print(f"[green]wrote[/] {len(df)} rows -> {p}")


@data_app.command("fetch-onchain")
def fetch_onchain_cmd(metric: str = "mvrv", days: int = 365 * 3, ctx: typer.Context = None) -> None:
    """Pull a Coin Metrics community-API metric and persist it."""

    from qt.data.onchain import fetch_coinmetrics
    from qt.data.store import ParquetStore

    settings = ctx.obj
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    df = fetch_coinmetrics(metric, since=since)
    store = ParquetStore(settings.data.parquet_dir)
    p = store.upsert("onchain", f"coinmetrics_{metric}", df)
    console.print(f"[green]wrote[/] {len(df)} rows -> {p}")


@data_app.command("fetch-fear-greed")
def fetch_fng_cmd(ctx: typer.Context = None) -> None:
    from qt.data.sentiment import fetch_fear_greed
    from qt.data.store import ParquetStore

    settings = ctx.obj
    df = fetch_fear_greed(limit=0)
    store = ParquetStore(settings.data.parquet_dir)
    p = store.upsert("sentiment", "fear_greed", df)
    console.print(f"[green]wrote[/] {len(df)} rows -> {p}")


@app.command("backtest")
def backtest_cmd(
    ohlcv_key: str = typer.Option("binance_BTCUSDT_1h", help="Key into ohlcv parquet"),
    initial_cash: float = 100_000.0,
    ctx: typer.Context = None,
) -> None:
    """Run a backtest using whatever local Parquet data is available."""

    from qt.backtest.engine import Backtester
    from qt.data.store import ParquetStore
    from qt.monitoring.reporting import format_backtest_report

    settings = ctx.obj
    store = ParquetStore(settings.data.parquet_dir)
    ohlcv = store.read("ohlcv", ohlcv_key)
    if ohlcv.empty:
        console.print(f"[red]no OHLCV at key={ohlcv_key}[/]; run `qt data fetch-ohlcv` first")
        raise typer.Exit(2)
    # Soft-load auxiliary inputs (any missing series degrades the score gracefully)
    def _read(ds: str, key: str, col: str | None = None):
        d = store.read(ds, key)
        if d.empty:
            return None
        if col and col in d.columns:
            return d[col]
        return d.iloc[:, 0] if d.shape[1] == 1 else d

    bt = Backtester(
        thresholds=settings.thresholds,
        risk_cfg=settings.risk,
        initial_cash=initial_cash,
    )
    result = bt.run(
        ohlcv=ohlcv,
        fear_greed=_read("sentiment", "fear_greed", "fear_greed"),
        mvrv_z=_read("onchain", "glassnode_mvrv_z", "mvrv_z"),
        sopr=_read("onchain", "glassnode_sopr_adj", "sopr_adj"),
    )
    format_backtest_report(result, console)


@app.command("info")
def info_cmd(ctx: typer.Context = None) -> None:
    """Show effective configuration (with secrets redacted)."""

    settings = ctx.obj
    cfg = settings.model_dump()
    for k in list(cfg):
        if "key" in k or "secret" in k or "passphrase" in k or "token" in k:
            cfg[k] = "***" if cfg[k] else ""
    console.print_json(data=cfg, default=str)


def main() -> None:  # pragma: no cover - entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
