"""Typer CLI for QT: fetch data, run backtests, paper trade.

Usage examples (after `pip install -e .`)::

    qt fetch-ohlcv --exchange binance --symbol BTC/USDT --timeframe 1h --days 365
    qt fetch-onchain --metric mvrv
    qt --config config/default.yaml backtest
    qt paper run --duration 1h
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from qt.core.config import Settings, load_settings
from qt.core.logging import configure_logging, get_logger

app = typer.Typer(help="QT — BTC quantitative trading platform")
data_app = typer.Typer(help="Data ingestion commands")
monitor_app = typer.Typer(help="Monitoring and health commands")
strategy_app = typer.Typer(help="Solution-gallery batch backtests (sim package)")
app.add_typer(data_app, name="data")
app.add_typer(monitor_app, name="monitor")
app.add_typer(strategy_app, name="strategy")

log = get_logger(__name__)
console = Console()


def _settings_from_ctx(ctx: typer.Context) -> Settings:
    if isinstance(ctx.obj, Settings):
        return ctx.obj
    return load_settings()


@app.callback()
def _bootstrap(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option(help="Path to YAML config file")] = None,
    log_level: Annotated[str, typer.Option(help="Log level")] = "INFO",
) -> None:
    configure_logging(level=log_level)
    settings = load_settings(config) if config else load_settings()
    ctx.obj = settings


@data_app.command("fetch-ohlcv")
def fetch_ohlcv_cmd(
    ctx: typer.Context,
    exchange: str = "binance",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    days: int = 365,
) -> None:
    """Fetch and persist OHLCV history to the local Parquet store."""

    from qt.data.market import fetch_ohlcv
    from qt.data.store import ParquetStore

    settings = _settings_from_ctx(ctx)
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    df = fetch_ohlcv(exchange, symbol, timeframe, since=since)
    key = f"{exchange}_{symbol.replace('/', '')}_{timeframe}"
    store = ParquetStore(settings.data.parquet_dir)
    p = store.upsert("ohlcv", key, df)
    console.print(f"[green]wrote[/] {len(df)} rows -> {p}")


@data_app.command("fetch-onchain")
def fetch_onchain_cmd(
    ctx: typer.Context,
    metric: str = "mvrv",
    days: int = 365 * 3,
) -> None:
    """Pull a Coin Metrics community-API metric and persist it."""

    from qt.data.onchain import fetch_coinmetrics
    from qt.data.store import ParquetStore

    settings = _settings_from_ctx(ctx)
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    df = fetch_coinmetrics(metric, since=since)
    store = ParquetStore(settings.data.parquet_dir)
    p = store.upsert("onchain", f"coinmetrics_{metric}", df)
    console.print(f"[green]wrote[/] {len(df)} rows -> {p}")


@data_app.command("fetch-fear-greed")
def fetch_fng_cmd(ctx: typer.Context) -> None:
    from qt.data.sentiment import fetch_fear_greed
    from qt.data.store import ParquetStore

    settings = _settings_from_ctx(ctx)
    df = fetch_fear_greed(limit=0)
    store = ParquetStore(settings.data.parquet_dir)
    p = store.upsert("sentiment", "fear_greed", df)
    console.print(f"[green]wrote[/] {len(df)} rows -> {p}")


@data_app.command("sources")
def data_sources_cmd(ctx: typer.Context) -> None:
    """Show configured data sources and local parquet freshness."""

    from qt.data.catalog import data_source_statuses
    from qt.data.store import ParquetStore

    settings = _settings_from_ctx(ctx)
    rows = data_source_statuses(ParquetStore(settings.data.parquet_dir))
    table = Table(title="Data sources")
    for column in ["status", "source", "group", "used_for", "store_key", "rows", "last_seen"]:
        table.add_column(column)
    for row in rows:
        fresh = row.get("fresh")
        exists = bool(row.get("exists"))
        configured = bool(row.get("configured"))
        if fresh is True:
            status = "fresh"
        elif exists:
            status = "stale"
        elif configured:
            status = "missing"
        else:
            status = "needs key"
        table.add_row(
            status,
            str(row["name"]),
            str(row["category"]),
            str(row["used_for"]),
            f"{row['dataset']}/{row['key']}",
            str(row["rows"]),
            str(row.get("end") or ""),
        )
    console.print(table)


@app.command("backtest")
def backtest_cmd(
    ctx: typer.Context,
    ohlcv_key: Annotated[
        str, typer.Option(help="Key into ohlcv parquet")
    ] = "binance_BTCUSDT_1h",
    initial_cash: float = 100_000.0,
    output_dir: Annotated[
        Path, typer.Option(help="Backtest artifact directory")
    ] = Path("data/backtests"),
) -> None:
    """Run a backtest using whatever local Parquet data is available."""

    from qt.backtest.artifacts import write_backtest_artifacts
    from qt.backtest.engine import Backtester
    from qt.data.store import ParquetStore
    from qt.monitoring.reporting import format_backtest_report

    settings = _settings_from_ctx(ctx)
    store = ParquetStore(settings.data.parquet_dir)
    ohlcv = store.read("ohlcv", ohlcv_key)
    if ohlcv.empty:
        console.print(f"[red]no OHLCV at key={ohlcv_key}[/]; run `qt data fetch-ohlcv` first")
        raise typer.Exit(2)
    # Soft-load auxiliary inputs (any missing series degrades the score gracefully)
    def _read(ds: str, key: str, col: str | None = None) -> pd.Series | pd.DataFrame | None:
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
        funding=_read("derivatives", "binance_BTCUSDT_funding", "funding_rate"),
        oi=_read("derivatives", "binance_BTCUSDT_oi_1h", "oi_usd"),
        long_short_ratio=_read("derivatives", "binance_BTCUSDT_lsr_1h", "long_short_ratio"),
        fear_greed=_read("sentiment", "fear_greed", "fear_greed"),
        mvrv_z=_read("onchain", "glassnode_mvrv_z", "mvrv_z"),
        sopr=_read("onchain", "glassnode_sopr_adj", "sopr_adj"),
        nupl=_read("onchain", "glassnode_nupl", "nupl"),
        puell=_read("onchain", "glassnode_puell_multiple", "puell_multiple"),
        reserve_risk=_read("onchain", "glassnode_reserve_risk", "reserve_risk"),
        exchange_netflow=_read("onchain", "glassnode_exchange_netflow", "exchange_netflow"),
        social_sentiment=_read(
            "sentiment", "santiment_sentiment_weighted_total_btc", "sentiment_weighted_total_btc"
        ),
        vix=_read("macro", "fred_vix", "vix"),
        dxy=_read("macro", "fred_dxy", "dxy"),
    )
    format_backtest_report(result, console)
    artifact = write_backtest_artifacts(
        result,
        output_dir,
        ohlcv_key=ohlcv_key,
        initial_cash=initial_cash,
        sources={
            "ohlcv": ohlcv_key,
            "funding": "binance_BTCUSDT_funding",
            "oi": "binance_BTCUSDT_oi_1h",
            "long_short_ratio": "binance_BTCUSDT_lsr_1h",
            "fear_greed": "fear_greed",
        },
    )
    console.print(f"[green]artifacts[/] {artifact.run_dir}")


@strategy_app.command("run")
def strategy_run_cmd(
    which: str = typer.Argument(..., help="One of: dca, capitulation, trend, carry"),
    ohlcv_key: str = typer.Option(
        "binance_BTCUSDT_1h", help="Key into the ohlcv parquet store",
    ),
    initial_cash: float = 10_000.0,
    ctx: typer.Context = None,
) -> None:
    """Backtest one of the four sim-package strategies on local data."""

    from qt.data.store import ParquetStore
    from qt.strategies.sim import (
        BasisCarryBacktest,
        BasisCarryConfig,
        SmartDCABacktest,
        SmartDCAConfig,
        WeeklyTrendBacktest,
        WeeklyTrendConfig,
    )

    settings = ctx.obj
    store = ParquetStore(settings.data.parquet_dir)
    ohlcv = store.read("ohlcv", ohlcv_key)
    if ohlcv.empty:
        console.print(f"[red]no OHLCV at key={ohlcv_key}[/]; fetch first")
        raise typer.Exit(2)

    def _read(ds: str, key: str, col: str | None = None):
        d = store.read(ds, key)
        if d.empty:
            return None
        if col and col in d.columns:
            return d[col]
        return d.iloc[:, 0] if d.shape[1] == 1 else d

    which = which.lower()
    if which in {"a", "dca", "smart_dca"}:
        out = SmartDCABacktest(SmartDCAConfig(initial_cash=initial_cash)).run(
            ohlcv,
            fear_greed=_read("sentiment", "fear_greed", "fear_greed"),
            mvrv_z=_read("onchain", "glassnode_mvrv_z", "mvrv_z"),
            nupl=_read("onchain", "glassnode_nupl", "nupl"),
        )
    elif which in {"c", "trend", "weekly", "weekly_trend"}:
        out = WeeklyTrendBacktest(WeeklyTrendConfig(initial_cash=initial_cash)).run(ohlcv)
    elif which in {"d", "carry", "basis", "basis_carry"}:
        funding = _read("derivatives", "binance_funding", "funding_rate")
        if funding is None:
            console.print("[red]basis carry requires funding-rate history[/]")
            raise typer.Exit(2)
        out = BasisCarryBacktest(BasisCarryConfig(initial_cash=initial_cash)).run(
            ohlcv, funding=funding,
        )
    elif which in {"b", "cap", "capitulation"}:
        console.print(
            "[yellow]capitulation batch backtest was removed in the merge; "
            "use `qt backtest` for the live capitulation engine[/]"
        )
        raise typer.Exit(2)
    else:
        console.print(f"[red]unknown strategy[/] {which!r}")
        raise typer.Exit(2)

    eq = out.equity
    cagr_factor = (eq.iloc[-1] / eq.iloc[0]) if eq.iloc[0] > 0 else float("nan")
    max_dd = ((eq / eq.cummax()) - 1.0).min()
    console.print(
        f"[green]strategy={which}[/] "
        f"final={eq.iloc[-1]:,.2f} "
        f"x={cagr_factor:.2f} "
        f"max_dd={max_dd:.2%} "
        f"trades={len(out.trades)}"
    )


@app.command("info")
def info_cmd(ctx: typer.Context) -> None:
    """Show effective configuration (with secrets redacted)."""

    settings = _settings_from_ctx(ctx)
    cfg = settings.model_dump()
    for k in list(cfg):
        if "key" in k or "secret" in k or "passphrase" in k or "token" in k:
            cfg[k] = "***" if cfg[k] else ""
    console.print_json(data=cfg, default=str)


@app.command("dashboard")
def dashboard_cmd(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Bind host")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port")] = 8765,
    backtests_dir: Annotated[
        Path, typer.Option(help="Backtest artifact directory")
    ] = Path("data/backtests"),
    monitor_state: Annotated[
        Path, typer.Option(help="Monitor heartbeat JSON")
    ] = Path("data/runtime/monitor_state.json"),
) -> None:
    """Serve the local dashboard for sources, monitor state, and backtests."""

    from qt.dashboard import serve_dashboard

    settings = _settings_from_ctx(ctx)
    console.print(f"[green]dashboard[/] http://{host}:{port}")
    serve_dashboard(
        host=host,
        port=port,
        parquet_dir=settings.data.parquet_dir,
        backtests_dir=backtests_dir,
        monitor_state_path=monitor_state,
    )


@monitor_app.command("health")
def monitor_health_cmd(
    state_path: Annotated[
        Path, typer.Option(help="Monitor heartbeat JSON")
    ] = Path("data/runtime/monitor_state.json"),
    stale_after_seconds: Annotated[
        int, typer.Option(help="Maximum acceptable heartbeat age")
    ] = 7200,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON output")] = False,
) -> None:
    """Check whether the long-running monitor heartbeat is healthy."""

    from qt.monitoring.health import evaluate_monitor_health

    health = evaluate_monitor_health(state_path, stale_after_seconds=stale_after_seconds)
    if json_output:
        console.print_json(data=health.as_dict())
    else:
        style = "green" if health.ok else "red"
        table = Table(title="Monitor health")
        table.add_column("Field")
        table.add_column("Value")
        for key, value in health.as_dict().items():
            table.add_row(key, "" if value is None else str(value))
        console.print(table)
        console.print(f"[{style}]{health.message}[/]")
    if not health.ok:
        raise typer.Exit(1)


def main() -> None:  # pragma: no cover - entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
