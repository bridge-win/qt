"""Standalone command-line interface."""

from __future__ import annotations

import hashlib
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
from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    DataSegment,
    MarketDataset,
    Timeframe,
)
from btc_backtest.data.providers.base import (
    MarketDataProvider,
    ProviderMetadata,
    ProviderRegistry,
)
from btc_backtest.data.providers.binance_archive import BinanceArchiveProvider
from btc_backtest.data.providers.bitstamp import BitstampProvider
from btc_backtest.data.providers.ccxt import CCXTProvider
from btc_backtest.data.providers.local import LocalParquetProvider
from btc_backtest.data.providers.synthetic import SyntheticProvider
from btc_backtest.data.validation import frame_fingerprint, validate_ohlcv
from btc_backtest.engine.models import BacktestResult, BacktestSpec
from btc_backtest.errors import BacktestError, ProviderError
from btc_backtest.reporting.artifacts import ArtifactBundle, ArtifactWriter
from btc_backtest.reporting.metrics import PerformanceMetrics, compute_metrics
from btc_backtest.signals.models import RankedSignal, SignalQuery
from btc_backtest.signals.providers.local import SignalArchiveProvider
from btc_backtest.signals.ranking import SignalAggregator
from btc_backtest.signals.store import SignalStore
from btc_backtest.strategies.base import StrategyMetadata
from btc_backtest.strategies.loader import (
    discover_entry_point_strategies,
    load_strategy,
)
from btc_backtest.strategies.registry import default_strategy_registry
from btc_backtest.validation.models import (
    ValidationResult,
    ValidationSpec,
    ValidationSplit,
)
from btc_backtest.validation.splits import purged_splits
from btc_backtest.validation.walk_forward import (
    ParameterCandidate,
    WalkForwardValidator,
)

app = typer.Typer(help="Independent BTC backtesting")
data_app = typer.Typer(help="Synchronize and inspect immutable market data")
signals_app = typer.Typer(help="Collect and rank point-in-time signals")
strategies_app = typer.Typer(help="Discover installed strategy plugins")
app.add_typer(data_app, name="data")
app.add_typer(signals_app, name="signals")
app.add_typer(strategies_app, name="strategies")
DEFAULT_CACHE_DIR = Path(".btc-backtest-cache")
DEFAULT_SIGNAL_STORE_DIR = DEFAULT_CACHE_DIR / "signals"


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
    years: int | None = typer.Option(None, min=1),
    market: str = typer.Option("spot"),
    cache_dir: Annotated[Path, typer.Option()] = DEFAULT_CACHE_DIR,
    path: Annotated[
        Path | None,
        typer.Option(help="Local Parquet path"),
    ] = None,
    synthetic: bool = typer.Option(
        False,
        "--synthetic",
        help="Allow explicitly labeled synthetic fixture data",
    ),
    allow_synthetic: bool = typer.Option(
        False,
        "--allow-synthetic",
        hidden=True,
    ),
    seed: int = typer.Option(7),
) -> None:
    """Fetch, validate, and atomically cache one market dataset."""

    try:
        with ExitStack() as stack:
            active_provider = _provider(
                provider,
                path=path,
                allow_synthetic=synthetic or allow_synthetic,
                seed=seed,
                stack=stack,
            )
            request = _request(
                provider=active_provider.metadata.id,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                years=years,
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
            years=None,
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
    output_format: Annotated[
        str,
        typer.Option("--format", help="json or table"),
    ] = "json",
) -> None:
    """List the complete built-in catalog and installed plugins."""

    try:
        DataCache(cache_dir)
        registry = default_strategy_registry()
        metadata = [
            registry.describe(strategy_id)
            for strategy_id in registry.list()
        ]
        discovered = discover_entry_point_strategies()
        builtin_ids = set(registry.list())
        metadata.extend(
            strategy.metadata
            for strategy_id, strategy in sorted(discovered.items())
            if strategy_id not in builtin_ids
        )
        _emit_strategy_metadata(metadata, output_format)
    except (BacktestError, OSError, ValueError) as error:
        _fail(error)


@strategies_app.command("describe")
def strategies_describe(
    strategy_id: str = typer.Argument(...),
    output_format: Annotated[
        str,
        typer.Option("--format", help="json or table"),
    ] = "json",
) -> None:
    """Describe one built-in or installed strategy."""

    try:
        registry = default_strategy_registry()
        if strategy_id in registry.list():
            metadata = registry.describe(strategy_id)
        else:
            discovered = discover_entry_point_strategies()
            strategy = discovered.get(strategy_id)
            if strategy is None:
                raise ValueError(f"unknown strategy: {strategy_id}")
            metadata = strategy.metadata
        _emit_strategy_metadata([metadata], output_format, single=True)
    except (BacktestError, OSError, ValueError) as error:
        _fail(error)


@signals_app.command("collect")
def signals_collect(
    archive: Annotated[
        Path,
        typer.Option(help="Immutable JSON, CSV, or Parquet signal archive"),
    ],
    symbol: str = typer.Option("BTC/USD"),
    horizon: str = typer.Option("1d"),
    start: str = typer.Option(...),
    end: str = typer.Option(...),
    store_dir: Annotated[
        Path,
        typer.Option("--store", help="SignalStore archive directory"),
    ] = DEFAULT_SIGNAL_STORE_DIR,
    source_type: Annotated[
        list[str] | None,
        typer.Option("--source-type", help="Optional source-type filter"),
    ] = None,
) -> None:
    """Collect normalized archive observations into an immutable store."""

    try:
        query = _signal_query(
            symbol=symbol,
            horizon=horizon,
            start=start,
            end=end,
            source_types=source_type,
        )
        provider = SignalArchiveProvider(archive)
        observations = provider.fetch(query)
        fingerprint = SignalStore(store_dir).append(observations)
        _emit_json(
            {
                "fingerprint": fingerprint,
                "observation_count": len(observations),
                "store": str(store_dir),
            }
        )
    except (BacktestError, OSError, ValueError) as error:
        _fail(error)


@signals_app.command("top")
def signals_top(
    archive: Annotated[
        Path,
        typer.Option(help="Immutable JSON, CSV, or Parquet signal archive"),
    ],
    symbol: str = typer.Option("BTC/USD"),
    horizon: str = typer.Option("1d"),
    start: str = typer.Option(...),
    end: str = typer.Option(...),
    as_of: str = typer.Option(...),
    source_type: Annotated[
        list[str] | None,
        typer.Option("--source-type", help="Optional source-type filter"),
    ] = None,
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit ranked signals as JSON",
    ),
) -> None:
    """Rank top available signals at one point in time."""

    try:
        query = _signal_query(
            symbol=symbol,
            horizon=horizon,
            start=start,
            end=end,
            source_types=source_type,
        )
        observations = SignalArchiveProvider(archive).fetch(query)
        ranked = SignalAggregator().rank(
            observations,
            as_of=_timestamp(as_of, "as-of"),
        )
        if json_output:
            _emit_json([item.model_dump(mode="json") for item in ranked])
            return
        _emit_ranked_signals(ranked)
    except (BacktestError, OSError, ValueError) as error:
        _fail(error)


@app.command("run")
def run_builtin(
    strategy_id: str = typer.Argument(..., help="Built-in strategy id"),
    provider: str = typer.Option("local"),
    symbol: str = typer.Option("BTC/USD"),
    timeframe: str = typer.Option("1d"),
    start: str | None = typer.Option(None),
    end: str | None = typer.Option(None),
    years: int | None = typer.Option(None, min=1),
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
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Artifact output root; defaults to CACHE_DIR/runs",
        ),
    ] = None,
    json_output: bool = typer.Option(True, "--json/--no-json"),
    synthetic: bool = typer.Option(
        False,
        "--synthetic",
        help="Allow explicitly labeled synthetic fixture data",
    ),
    allow_synthetic: bool = typer.Option(
        False,
        "--allow-synthetic",
        hidden=True,
    ),
    seed: int = typer.Option(7),
) -> None:
    """Run one built-in strategy and write a reproducible artifact bundle."""

    try:
        registry = default_strategy_registry()
        parameters = _parameters(params_json)
        with ExitStack() as stack:
            active_provider = _provider(
                provider,
                path=path,
                allow_synthetic=synthetic or allow_synthetic,
                seed=seed,
                stack=stack,
            )
            request = _request(
                provider=active_provider.metadata.id,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                years=years,
                market=market,
                path=path,
                require_real=active_provider.metadata.real_data,
            )
            spec = BacktestSpec(
                strategy=strategy_id,
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
                strategy_registry=registry.factories,
                cache=DataCache(cache_dir),
            ).run(spec)
        _emit_run_payload(
            result,
            spec=spec,
            output_root=output or cache_dir / "runs",
            json_output=json_output,
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
    years: int | None = typer.Option(None, min=1),
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
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Artifact output root; defaults to CACHE_DIR/runs",
        ),
    ] = None,
    json_output: bool = typer.Option(True, "--json/--no-json"),
    synthetic: bool = typer.Option(
        False,
        "--synthetic",
        help="Allow explicitly labeled synthetic fixture data",
    ),
    allow_synthetic: bool = typer.Option(
        False,
        "--allow-synthetic",
        hidden=True,
    ),
    seed: int = typer.Option(7),
) -> None:
    """Run one explicit external strategy and write an artifact bundle."""

    try:
        strategy = load_strategy(strategy_reference)
        parameters = _parameters(params_json)
        with ExitStack() as stack:
            active_provider = _provider(
                provider,
                path=path,
                allow_synthetic=synthetic or allow_synthetic,
                seed=seed,
                stack=stack,
            )
            request = _request(
                provider=active_provider.metadata.id,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                years=years,
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
        _emit_run_payload(
            result,
            spec=spec,
            output_root=output or cache_dir / "runs",
            json_output=json_output,
        )
    except (BacktestError, OSError, ValueError) as error:
        _fail(error)


@app.command("validate")
def validate_builtin(
    strategy_id: str = typer.Argument(..., help="Built-in strategy id"),
    provider: str = typer.Option("local"),
    symbol: str = typer.Option("BTC/USD"),
    timeframe: str = typer.Option("1d"),
    start: str | None = typer.Option(None),
    end: str | None = typer.Option(None),
    years: int | None = typer.Option(None, min=1),
    market: str = typer.Option("spot"),
    path: Annotated[
        Path | None,
        typer.Option(help="Local Parquet path"),
    ] = None,
    cache_dir: Annotated[Path, typer.Option()] = DEFAULT_CACHE_DIR,
    params_json: str = typer.Option("{}"),
    candidate_json: Annotated[
        list[str] | None,
        typer.Option(
            "--candidate-json",
            help="JSON object candidate parameters; repeat for a grid",
        ),
    ] = None,
    initial_cash: str = typer.Option("10000"),
    fee_bps: str = typer.Option("10"),
    slippage_bps: str = typer.Option("5"),
    train_bars: int = typer.Option(365, min=1),
    test_bars: int = typer.Option(90, min=1),
    purge_bars: int = typer.Option(0, min=0),
    embargo_bars: int = typer.Option(0, min=0),
    final_test_bars: int = typer.Option(365, min=1),
    objective: str = typer.Option("sharpe"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Artifact output root; defaults to CACHE_DIR/runs",
        ),
    ] = None,
    json_output: bool = typer.Option(True, "--json/--no-json"),
    synthetic: bool = typer.Option(
        False,
        "--synthetic",
        help="Allow explicitly labeled synthetic fixture data",
    ),
    allow_synthetic: bool = typer.Option(
        False,
        "--allow-synthetic",
        hidden=True,
    ),
    seed: int = typer.Option(7),
) -> None:
    """Run walk-forward validation and write the final artifact bundle."""

    try:
        registry = default_strategy_registry()
        parameters = _parameters(params_json)
        with ExitStack() as stack:
            active_provider = _provider(
                provider,
                path=path,
                allow_synthetic=synthetic or allow_synthetic,
                seed=seed,
                stack=stack,
            )
            request = _request(
                provider=active_provider.metadata.id,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                years=years,
                market=market,
                path=path,
                require_real=active_provider.metadata.real_data,
            )
            cache = DataCache(cache_dir)
            provider_registry = ProviderRegistry([active_provider])
            dataset = provider_registry.fetch(request, cache)
            validation_provider = _WindowedDatasetProvider(
                active_provider.metadata,
                dataset,
            )
            runner = BacktestRunner(
                provider_registry={
                    validation_provider.metadata.id: validation_provider,
                },
                strategy_registry=registry.factories,
                cache=cache,
            )
            base_spec = BacktestSpec(
                strategy=strategy_id,
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
            (
                validation_spec,
                splits,
                final_start,
                final_end,
            ) = _validation_windows(
                dataset.frame.index,
                request,
                train_bars=train_bars,
                test_bars=test_bars,
                purge_bars=purge_bars,
                embargo_bars=embargo_bars,
                final_test_bars=final_test_bars,
                objective=objective,
                seed=seed,
            )
            candidates = _parameter_candidates(candidate_json, parameters)
            walk_forward = WalkForwardValidator(
                runner,
                validation_spec,
                splits=splits,
            ).run(base_spec, candidates)
            final_parameters = (
                dict(walk_forward.final_evaluation.selected_candidate.parameters)
                if walk_forward.final_evaluation is not None
                else dict(parameters)
            )
            final_spec = base_spec.model_copy(
                update={
                    "strategy_params": final_parameters,
                    "data": request.model_copy(
                        update={"start": final_start, "end": final_end}
                    ),
                }
            )
            final_result = runner.run(final_spec)
        validation = ValidationResult(
            spec=validation_spec,
            splits=splits,
            selected_parameters=tuple(
                dict(item)
                for item in walk_forward.selected_parameters
            ),
            warnings=(),
        )
        _emit_run_payload(
            final_result,
            spec=final_spec,
            output_root=output or cache_dir / "runs",
            validation=validation,
            walk_forward=walk_forward.model_dump(mode="json"),
            json_output=json_output,
        )
    except (BacktestError, OSError, ValueError) as error:
        _fail(error)


def _emit_run_payload(
    result: BacktestResult,
    *,
    spec: BacktestSpec,
    output_root: Path,
    validation: ValidationResult | None = None,
    walk_forward: Mapping[str, object] | None = None,
    json_output: bool,
) -> None:
    enriched = _result_with_cli_diagnostics(result, spec)
    metrics = compute_metrics(enriched)
    bundle = ArtifactWriter().write(
        enriched,
        metrics,
        validation or _single_run_validation(spec),
        output_root,
    )
    payload = _run_payload(enriched, metrics, bundle)
    if validation is not None:
        payload["validation"] = validation.model_dump(mode="json")
    if walk_forward is not None:
        payload["walk_forward"] = dict(walk_forward)
    if json_output:
        _emit_json(payload)
        return
    typer.echo(f"strategy_id={enriched.strategy_id}")
    typer.echo(f"artifact_dir={bundle.run_dir}")
    typer.echo(f"total_return={metrics.total_return}")


def _result_with_cli_diagnostics(
    result: BacktestResult,
    spec: BacktestSpec,
) -> BacktestResult:
    diagnostics = dict(result.diagnostics)
    diagnostics["strategy_parameters"] = dict(spec.strategy_params)
    return result.model_copy(update={"diagnostics": diagnostics})


def _run_payload(
    result: BacktestResult,
    metrics: PerformanceMetrics,
    bundle: ArtifactBundle,
) -> dict[str, object]:
    payload = result.model_dump(mode="json")
    payload["metrics"] = metrics.model_dump(mode="json")
    payload["artifact_dir"] = str(bundle.run_dir)
    payload["synthetic"] = any(
        not manifest.real_data
        for manifest in result.data_manifests
    )
    return payload


def _single_run_validation(spec: BacktestSpec) -> ValidationResult:
    delta = _bar_delta(spec.data.timeframe)
    final_start = spec.data.start + delta
    if final_start >= spec.data.end:
        raise ValueError(
            "run artifact validation requires at least two bars in the interval"
        )
    return ValidationResult(
        spec=ValidationSpec(
            selection_end=spec.data.start,
            final_test_start=final_start,
            final_test_end=spec.data.end,
            objective="single_run",
            seed=spec.seed,
        ),
        splits=(),
        selected_parameters=(),
        warnings=("single run; walk-forward validation was not requested",),
    )


def _parameter_candidates(
    values: list[str] | None,
    fallback: Mapping[str, object],
) -> tuple[ParameterCandidate, ...]:
    if not values:
        return (ParameterCandidate(parameters=dict(fallback)),)
    return tuple(
        ParameterCandidate(parameters=_parameters(value))
        for value in values
    )


def _validation_windows(
    index: pd.Index,
    request: DataRequest,
    *,
    train_bars: int,
    test_bars: int,
    purge_bars: int,
    embargo_bars: int,
    final_test_bars: int,
    objective: str,
    seed: int,
) -> tuple[ValidationSpec, tuple[ValidationSplit, ...], datetime, datetime]:
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError("validation requires a DatetimeIndex dataset")
    normalized = index.tz_convert(timezone.utc)
    if len(normalized) <= final_test_bars:
        raise ValueError("validation dataset is shorter than final-test window")
    final_start = normalized[-final_test_bars].to_pydatetime()
    final_end = request.end
    selection_index = normalized[normalized < final_start]
    if selection_index.empty:
        raise ValueError("validation requires selection data before final test")
    splits = purged_splits(
        selection_index,
        train_bars=train_bars,
        test_bars=test_bars,
        purge_bars=purge_bars,
        embargo_bars=embargo_bars,
    )
    if not splits:
        raise ValueError("validation parameters produced no walk-forward splits")
    validation_spec = ValidationSpec(
        selection_end=selection_index[-1].to_pydatetime(),
        final_test_start=final_start,
        final_test_end=final_end,
        train_bars=train_bars,
        test_bars=test_bars,
        purge_bars=purge_bars,
        embargo_bars=embargo_bars,
        objective=objective,
        seed=seed,
    )
    return validation_spec, splits, final_start, final_end


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
                "synthetic fixture data requires --synthetic; real data "
                "coverage never falls back to generated data"
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


class _WindowedDatasetProvider:
    """Serve exact validation subwindows from one already-fetched dataset."""

    def __init__(
        self,
        metadata: ProviderMetadata,
        dataset: MarketDataset,
    ) -> None:
        self._metadata = metadata
        self._dataset = dataset

    @property
    def metadata(self) -> ProviderMetadata:
        return self._metadata

    def fetch(self, request: DataRequest) -> MarketDataset:
        if request.provider != self._metadata.id:
            raise ProviderError(
                f"windowed provider cannot satisfy provider {request.provider}"
            )
        if request.require_real and not self._dataset.manifest.real_data:
            raise ProviderError(
                "request requires real data but cached validation dataset is synthetic"
            )
        frame = self._dataset.frame.loc[
            (self._dataset.frame.index >= request.start)
            & (self._dataset.frame.index < request.end)
        ]
        normalized, gaps = validate_ohlcv(frame, request)
        delivered_start = normalized.index[0].to_pydatetime()
        delivered_end = (
            normalized.index[-1] + _bar_delta(request.timeframe)
        ).to_pydatetime()
        fingerprint = frame_fingerprint(normalized)
        raw_identity = json.dumps(
            {
                "source": self._dataset.manifest.normalized_sha256,
                "provider": request.provider,
                "market": request.market,
                "symbol": request.symbol,
                "timeframe": request.timeframe,
                "start": request.start.isoformat(),
                "end": request.end.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        segment = DataSegment(
            provider=request.provider,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            start=delivered_start,
            end=delivered_end,
            real_data=self._dataset.manifest.real_data,
            normalized_sha256=fingerprint,
            source=self._dataset.manifest.source,
        )
        manifest = DataManifest(
            provider=request.provider,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            requested_start=request.start,
            requested_end=request.end,
            delivered_start=delivered_start,
            delivered_end=delivered_end,
            retrieved_at=datetime.now(timezone.utc),
            real_data=self._dataset.manifest.real_data,
            raw_sha256=(hashlib.sha256(raw_identity).hexdigest(),),
            normalized_sha256=fingerprint,
            source=self._dataset.manifest.source,
            license_note=self._dataset.manifest.license_note,
            gaps=gaps,
            segments=(segment,),
        )
        return MarketDataset(frame=normalized, manifest=manifest)


def _request(
    *,
    provider: str,
    symbol: str,
    timeframe: str,
    start: str | None,
    end: str | None,
    years: int | None,
    market: str,
    path: Path | None,
    require_real: bool,
) -> DataRequest:
    typed_timeframe = _timeframe(timeframe)
    if years is not None:
        if start is not None:
            raise ProviderError("--years cannot be combined with --start")
        inferred_end = (
            _timestamp(end, "end")
            if end is not None
            else _current_interval_end(typed_timeframe)
        )
        inferred_start = _subtract_calendar_years(inferred_end, years)
    elif start is None or end is None:
        if provider != "local" or path is None:
            raise ProviderError(
                "--start and --end, or --years with optional --end, are "
                "required for non-local providers"
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


def _current_interval_end(timeframe: Timeframe) -> datetime:
    now = datetime.now(timezone.utc)
    if timeframe == "1h":
        return now.replace(minute=0, second=0, microsecond=0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _subtract_calendar_years(value: datetime, years: int) -> datetime:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        if value.month == 2 and value.day == 29:
            return value.replace(year=value.year - years, day=28)
        raise


def _bar_delta(timeframe: Timeframe) -> timedelta:
    if timeframe == "1h":
        return timedelta(hours=1)
    return timedelta(days=1)


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


def _signal_query(
    *,
    symbol: str,
    horizon: str,
    start: str,
    end: str,
    source_types: list[str] | None,
) -> SignalQuery:
    return SignalQuery(
        start=_timestamp(start, "start"),
        end=_timestamp(end, "end"),
        symbol=symbol,
        horizons=(horizon,),
        source_types=tuple(source_types or ()),
    )


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


def _emit_strategy_metadata(
    metadata: list[StrategyMetadata],
    output_format: str,
    *,
    single: bool = False,
) -> None:
    if output_format == "json":
        payload: object = (
            metadata[0].model_dump(mode="json")
            if single
            else [item.model_dump(mode="json") for item in metadata]
        )
        _emit_json(payload)
        return
    if output_format != "table":
        raise ValueError("format must be json or table")
    typer.echo("ID\tVERSION\tINSTRUMENTS\tDESCRIPTION")
    for item in metadata:
        instruments = ",".join(
            instrument.value
            for instrument in item.supported_instruments
        )
        typer.echo(
            f"{item.id}\t{item.version}\t{instruments}\t{item.description}"
        )


def _emit_ranked_signals(ranked: tuple[RankedSignal, ...]) -> None:
    typer.echo("SYMBOL\tHORIZON\tDIRECTION\tCONFIDENCE\tCONTRIBUTORS")
    for item in ranked:
        contributors = ",".join(
            contributor.observation_id
            for contributor in item.contributors
        )
        typer.echo(
            f"{item.symbol}\t{item.horizon}\t{item.direction}\t"
            f"{item.confidence}\t{contributors}"
        )


def _fail(error: Exception) -> NoReturn:
    typer.echo(f"{type(error).__name__}: {error}", err=True)
    raise typer.Exit(code=2)
