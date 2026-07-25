"""Standalone command-line interface."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, NoReturn, cast

import httpx
import pandas as pd
import typer

from btc_backtest.api import BacktestRunner
from btc_backtest.data.cache import DataCache
from btc_backtest.data.models import DataRequest, Timeframe
from btc_backtest.data.providers.base import (
    MarketDataProvider,
    ProviderRegistry,
)
from btc_backtest.data.providers.binance_archive import BinanceArchiveProvider
from btc_backtest.data.providers.bitstamp import BitstampProvider
from btc_backtest.data.providers.ccxt import CCXTProvider
from btc_backtest.data.providers.local import LocalParquetProvider
from btc_backtest.data.providers.synthetic import SyntheticProvider
from btc_backtest.engine.models import BacktestSpec
from btc_backtest.errors import BacktestError, ProviderError
from btc_backtest.strategies.loader import (
    discover_entry_point_strategies,
    load_strategy,
)

app = typer.Typer(help="Independent BTC backtesting")
data_app = typer.Typer(help="Synchronize and inspect immutable market data")
strategies_app = typer.Typer(help="Discover installed strategy plugins")
app.add_typer(data_app, name="data")
app.add_typer(strategies_app, name="strategies")
DEFAULT_CACHE_DIR = Path(".btc-backtest-cache")


@app.callback()
def main() -> None:
    """Run independent BTC backtesting workflows."""


@data_app.command("sync")
def data_sync(
    provider: str = typer.Option(..., help="Provider id"),
    symbol: str = typer.Option("BTC/USD"),
    timeframe: str = typer.Option("1d"),
    start: str | None = typer.Option(None),
    end: str | None = typer.Option(None),
    market: str = typer.Option("spot"),
    cache_dir: Annotated[Path, typer.Option()] = DEFAULT_CACHE_DIR,
    path: Annotated[
        Path | None,
        typer.Option(help="Local Parquet path"),
    ] = None,
    allow_synthetic: bool = typer.Option(False),
    seed: int = typer.Option(7),
) -> None:
    """Fetch, validate, and atomically cache one market dataset."""

    try:
        with ExitStack() as stack:
            active_provider = _provider(
                provider,
                path=path,
                allow_synthetic=allow_synthetic,
                seed=seed,
                stack=stack,
            )
            request = _request(
                provider=active_provider.metadata.id,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                market=market,
                path=path,
                require_real=active_provider.metadata.real_data,
            )
            dataset = ProviderRegistry([active_provider]).fetch(
                request,
                DataCache(cache_dir),
            )
        _emit_json(dataset.manifest.model_dump(mode="json"))
    except (BacktestError, OSError, ValueError) as error:
        _fail(error)


@data_app.command("inspect")
def data_inspect(
    provider: str = typer.Option(..., help="Provider id"),
    symbol: str = typer.Option("BTC/USD"),
    timeframe: str = typer.Option("1d"),
    start: str = typer.Option(...),
    end: str = typer.Option(...),
    market: str = typer.Option("spot"),
    cache_dir: Annotated[Path, typer.Option()] = DEFAULT_CACHE_DIR,
    allow_synthetic: bool = typer.Option(False),
) -> None:
    """Read and revalidate one published cache entry."""

    try:
        if provider == "synthetic" and not allow_synthetic:
            raise ProviderError(
                "synthetic cache inspection requires --allow-synthetic"
            )
        request = _request(
            provider=provider,
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            market=market,
            path=None,
            require_real=provider != "synthetic",
        )
        dataset = DataCache(cache_dir).load(request)
        if dataset is None:
            raise ProviderError("no published cache entry matches the request")
        _emit_json(dataset.manifest.model_dump(mode="json"))
    except (BacktestError, OSError, ValueError) as error:
        _fail(error)


@strategies_app.command("list")
def strategies_list(
    cache_dir: Annotated[Path, typer.Option()] = DEFAULT_CACHE_DIR,
) -> None:
    """List explicitly installed strategy entry points."""

    try:
        DataCache(cache_dir)
        discovered = discover_entry_point_strategies()
        _emit_json(
            [
                strategy.metadata.model_dump(mode="json")
                for _, strategy in sorted(discovered.items())
            ]
        )
    except (BacktestError, OSError, ValueError) as error:
        _fail(error)


@app.command("run-custom")
def run_custom(
    strategy_reference: str = typer.Argument(
        ...,
        help="Exact file.py:ClassName reference",
    ),
    provider: str = typer.Option("local"),
    symbol: str = typer.Option("BTC/USD"),
    timeframe: str = typer.Option("1d"),
    start: str | None = typer.Option(None),
    end: str | None = typer.Option(None),
    market: str = typer.Option("spot"),
    path: Annotated[
        Path | None,
        typer.Option(help="Local Parquet path"),
    ] = None,
    cache_dir: Annotated[Path, typer.Option()] = DEFAULT_CACHE_DIR,
    params_json: str = typer.Option("{}"),
    initial_cash: str = typer.Option("10000"),
    fee_bps: str = typer.Option("10"),
    slippage_bps: str = typer.Option("5"),
    allow_synthetic: bool = typer.Option(False),
    seed: int = typer.Option(7),
) -> None:
    """Run one explicit external strategy and print its result as JSON."""

    try:
        strategy = load_strategy(strategy_reference)
        parameters = _parameters(params_json)
        with ExitStack() as stack:
            active_provider = _provider(
                provider,
                path=path,
                allow_synthetic=allow_synthetic,
                seed=seed,
                stack=stack,
            )
            request = _request(
                provider=active_provider.metadata.id,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                market=market,
                path=path,
                require_real=active_provider.metadata.real_data,
            )
            spec = BacktestSpec(
                strategy=strategy.metadata.id,
                strategy_params=parameters,
                data=request,
                initial_cash=_decimal_option(initial_cash, "initial-cash"),
                fee_bps=_decimal_option(fee_bps, "fee-bps"),
                slippage_bps=_decimal_option(
                    slippage_bps,
                    "slippage-bps",
                ),
                seed=seed,
            )
            result = BacktestRunner(
                provider_registry={
                    active_provider.metadata.id: active_provider,
                },
                strategy_registry={},
                cache=DataCache(cache_dir),
            ).run(spec, strategy=strategy)
        _emit_json(result.model_dump(mode="json"))
    except (BacktestError, OSError, ValueError) as error:
        _fail(error)


def _provider(
    provider_id: str,
    *,
    path: Path | None,
    allow_synthetic: bool,
    seed: int,
    stack: ExitStack,
) -> MarketDataProvider:
    if provider_id == "local":
        if path is None:
            raise ProviderError("local provider requires --path")
        return LocalParquetProvider(path)
    if provider_id == "synthetic":
        if not allow_synthetic:
            raise ProviderError(
                "synthetic data requires the explicit --allow-synthetic flag"
            )
        return SyntheticProvider(seed)
    if provider_id.startswith("ccxt:"):
        exchange_id = provider_id.partition(":")[2]
        return CCXTProvider(exchange_id)

    client = stack.enter_context(httpx.Client(timeout=30.0))
    if provider_id == "bitstamp":
        return BitstampProvider(client)
    if provider_id == "binance_archive":
        return BinanceArchiveProvider(client)
    raise ProviderError(f"unknown provider: {provider_id}")


def _request(
    *,
    provider: str,
    symbol: str,
    timeframe: str,
    start: str | None,
    end: str | None,
    market: str,
    path: Path | None,
    require_real: bool,
) -> DataRequest:
    typed_timeframe = _timeframe(timeframe)
    if start is None or end is None:
        if provider != "local" or path is None:
            raise ProviderError(
                "--start and --end are required for non-local providers"
            )
        inferred_start, inferred_end = _local_interval(path, typed_timeframe)
    else:
        inferred_start = _timestamp(start, "start")
        inferred_end = _timestamp(end, "end")
    return DataRequest(
        provider=provider,
        symbol=symbol,
        timeframe=typed_timeframe,
        start=inferred_start,
        end=inferred_end,
        market=market,
        require_real=require_real,
    )


def _local_interval(
    path: Path,
    timeframe: Timeframe,
) -> tuple[datetime, datetime]:
    try:
        frame = pd.read_parquet(path)
    except (OSError, ValueError) as error:
        raise ProviderError(f"failed to inspect local Parquet file {path}") from error
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        raise ProviderError(
            "local Parquet data requires a non-empty DatetimeIndex"
        )
    if frame.index.tz is None:
        raise ProviderError("local Parquet timestamps must be timezone-aware")
    delta = timedelta(hours=1) if timeframe == "1h" else timedelta(days=1)
    return (
        frame.index.min().to_pydatetime(),
        (frame.index.max() + delta).to_pydatetime(),
    )


def _timestamp(value: str, label: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _timeframe(value: str) -> Timeframe:
    if value not in ("1h", "1d"):
        raise ValueError("timeframe must be 1h or 1d")
    return cast(Timeframe, value)


def _parameters(value: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("params-json must be valid JSON") from error
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) for key in parsed
    ):
        raise ValueError("params-json must contain a JSON object")
    return cast(dict[str, object], parsed)


def _decimal_option(value: str, label: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a decimal number") from error
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _emit_json(value: object) -> None:
    typer.echo(
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
    )


def _fail(error: Exception) -> NoReturn:
    typer.echo(f"{type(error).__name__}: {error}", err=True)
    raise typer.Exit(code=2)
