"""ResearchExecutor-compatible native Nautilus execution and artifact publication."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta, timezone
from decimal import Decimal
from itertools import pairwise
from math import isfinite, sqrt
from numbers import Integral, Real
from pathlib import Path
from typing import Protocol

import pandas as pd
from btc_backtest.data.models import DataRequest
from btc_backtest.engine.models import BacktestSpec
from btc_backtest.strategies.base import InitializationContext, Strategy

from qt.nautilus.models import (
    ExecutionAssumptions,
    NautilusArtifact,
    NautilusRunSummary,
)
from qt.nautilus.runner import InstrumentDefinition, NautilusDataFrameRunner
from qt.research.datasets import DatasetCatalog
from qt.research.service import build_strategy
from qt.strategy_ports.btcqt import BtcqtCausalBundle, BtcqtCausalRecord
from qt.workbench.auxiliary_datasets import (
    AuxiliaryDatasetCatalog,
    AuxiliaryReference,
    validate_auxiliary_selection,
)

ProgressCallback = Callable[[str, int], None]
CancellationCheck = Callable[[], bool]
JsonDict = dict[str, object]


@dataclass(frozen=True)
class LoadedAuxiliaryData:
    """Server-attested native inputs, stripped of file locations."""

    mark_quotes: pd.DataFrame | None
    causal_bundle: BtcqtCausalBundle | None


class PluginRuntime(Protocol):
    """Worker-injected boundary for immutable plugin execution only."""

    def execute_plugin(
        self,
        strategy_version: Mapping[str, object],
        experiment: Mapping[str, object],
        parameter_overrides: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict: ...


class NautilusResearchExecutor:
    """Execute one research spec with Nautilus, never with the legacy event runner."""

    def __init__(
        self,
        parquet_root: Path,
        artifact_root: Path,
        *,
        plugin_runtime: PluginRuntime | None = None,
    ) -> None:
        self.parquet_root = parquet_root
        self.artifact_root = artifact_root
        self.datasets = DatasetCatalog(parquet_root)
        self.auxiliary_datasets = AuxiliaryDatasetCatalog(parquet_root)
        self.runner = NautilusDataFrameRunner()
        self.plugin_runtime = plugin_runtime

    def execute(
        self,
        spec: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict:
        plugin = _lab_plugin_version(spec)
        if plugin is not None:
            if self.plugin_runtime is None:
                raise RuntimeError(
                    "plugin lab strategy requires an injected IsolatedPluginRuntime; "
                    "host-process execution is prohibited",
                )
            overrides = spec.get("parameter_overrides", {})
            if not isinstance(overrides, Mapping):
                raise ValueError("parameter_overrides must be an object")
            return self.plugin_runtime.execute_plugin(
                plugin,
                spec,
                overrides,
                progress,
                cancelled,
            )
        self._check_cancelled(cancelled)
        progress("data_validation", 5)
        dataset_id = str(spec["dataset_id"])
        dataset = self.datasets.get(dataset_id)
        requested_fingerprint = spec.get("dataset_fingerprint")
        if requested_fingerprint is not None and requested_fingerprint != dataset.get(
            "fingerprint"
        ):
            raise ValueError("dataset fingerprint changed; submit a new immutable research request")
        frame = self._load_frame(dataset_id)
        frame, decision_start = _execution_window(frame, spec)
        refreshed_dataset = self.datasets.get(dataset_id)
        if refreshed_dataset.get("fingerprint") != dataset.get("fingerprint"):
            raise ValueError(
                "dataset fingerprint changed while loading; submit a new immutable request"
            )
        if dataset.get("market") == "perpetual":
            raise ValueError(
                "perpetual and leveraged research are disabled pending independent "
                "quote/mark, margin, and liquidation acceptance",
            )
        assumptions = _assumptions(spec)
        auxiliary = self._load_auxiliary(spec, dataset)
        strategy = _native_strategy(
            build_strategy(
                spec,
                version_resolver=_embedded_version_resolver(spec),
                source_constructor=lambda version, parameters: self._source_constructor(
                    version,
                    parameters,
                    dataset_version=str(dataset["fingerprint"]),
                    causal_bundle=auxiliary.causal_bundle,
                    leverage=assumptions.leverage,
                ),
                primary_timeframe=str(dataset["timeframe"]),
            )
        )
        initialize = _initializer(strategy, spec, dataset, frame, assumptions)
        progress("native_engine", 25)
        native = self.runner.run(
            frame=frame,
            strategy=strategy,
            assumptions=assumptions,
            instrument=_instrument(dataset, spec, strategy=strategy),
            initialize=initialize,
            decision_start=decision_start,
            cancelled=cancelled,
            mark_quotes=auxiliary.mark_quotes,
        )
        self._check_cancelled(cancelled)
        progress("native_reports", 80)
        run_id = _run_id(spec, dataset, native.engine_version)
        summary = self._write_artifacts(run_id, native, strategy.metadata.id, assumptions)
        progress("completed", 100)
        return _result_payload(summary, native, dataset, spec)

    def _load_frame(self, dataset_id: str) -> pd.DataFrame:
        frame = pd.read_parquet(self.datasets.path_for(dataset_id)).sort_index()
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise ValueError("research parquet requires a DatetimeIndex")
        frame.index = (
            frame.index.tz_localize(timezone.utc)
            if frame.index.tz is None
            else frame.index.tz_convert(timezone.utc)
        )
        return frame

    def _load_auxiliary(
        self,
        spec: Mapping[str, object],
        dataset: Mapping[str, object],
    ) -> LoadedAuxiliaryData:
        """Re-attest durable IDs and make source records causal before execution."""
        raw_references = spec.get("auxiliary_datasets")
        if raw_references is None:
            raw_references = []
        if not isinstance(raw_references, list):
            raise ValueError("auxiliary_datasets must be an array of registered references")
        if len(raw_references) > 8:
            raise ValueError("at most eight auxiliary datasets may be attached to an experiment")
        loaded: list[tuple[AuxiliaryReference, pd.DataFrame]] = []
        for raw_reference in raw_references:
            if not isinstance(raw_reference, Mapping):
                raise ValueError("auxiliary dataset reference must be an object")
            loaded.append(self.auxiliary_datasets.load(raw_reference))
        references = [reference for reference, _frame in loaded]
        symbol = dataset.get("symbol")
        market = dataset.get("market")
        if not isinstance(symbol, str) or not isinstance(market, str):
            raise ValueError("primary dataset has no immutable symbol/market contract")
        required = {"mark_quotes"} if market == "perpetual" else set()
        if _is_btcqt_s2(spec):
            required.update({"funding_settlements", "open_interest"})
        validate_auxiliary_selection(
            references,
            primary_symbol=symbol,
            primary_market=market,
            required_kinds=frozenset(required),
        )
        by_kind = {reference.kind: frame for reference, frame in loaded}
        causal_streams: dict[str, tuple[str, tuple[BtcqtCausalRecord, ...]]] = {}
        for reference, frame in loaded:
            if reference.kind not in {"funding_settlements", "open_interest", "liquidations"}:
                continue
            causal_streams[reference.kind] = (
                reference.fingerprint,
                _causal_records(frame),
            )
        return LoadedAuxiliaryData(
            mark_quotes=by_kind.get("mark_quotes"),
            causal_bundle=(
                BtcqtCausalBundle.from_attested_streams(causal_streams)
                if causal_streams
                else None
            ),
        )

    @staticmethod
    def _source_constructor(
        version: Mapping[str, object],
        parameters: Mapping[str, object],
        *,
        dataset_version: str,
        causal_bundle: BtcqtCausalBundle | Mapping[str, object] | None,
        leverage: Decimal,
    ) -> Strategy:
        """Resolve preserved families through the native source bridge only.

        The concrete catalog/Qt5 constructors live in the strategy-port layer.
        Until a family exposes a true ``btc_backtest.Strategy`` plus native
        order/fill lifecycle, it fails closed instead of becoming a generic
        target-weight strategy.
        """

        from qt.workbench.source_constructors import build_source_strategy

        return build_source_strategy(
            version,
            parameters,
            dataset_version=dataset_version,
            causal_bundle=causal_bundle,
            leverage=leverage,
        )

    def _write_artifacts(
        self,
        run_id: str,
        native: object,
        strategy_id: str,
        assumptions: ExecutionAssumptions,
    ) -> NautilusRunSummary:
        directory = self.artifact_root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        reports = (
            _write_frame(directory, "orders.csv", native.orders),
            _write_frame(directory, "fills.csv", native.fills),
            _write_frame(directory, "positions.csv", native.positions),
            _write_frame(directory, "account.csv", native.account),
            _write_json(
                directory,
                "returns.json",
                _equity_returns(_equity_series(native.traces, native=native)),
            ),
            _write_json(directory, "equity.json", _equity_series(native.traces, native=native)),
            _write_json(directory, "decision_traces.json", _decision_traces(native.traces)),
        )
        started = native.started_at
        finished = native.finished_at
        return NautilusRunSummary(
            run_id=run_id,
            engine="nautilus_trader",
            engine_version=native.engine_version,
            strategy_id=strategy_id,
            data_requirement=assumptions.data_requirement,
            started_at=started
            if started.tzinfo is not None
            else started.replace(tzinfo=timezone.utc),
            finished_at=finished
            if finished.tzinfo is not None
            else finished.replace(tzinfo=timezone.utc),
            total_events=int(native.result.get("total_events", 0)),
            total_orders=int(native.result.get("total_orders", len(native.orders))),
            total_positions=int(native.result.get("total_positions", len(native.positions))),
            reports=reports,
            traces=native.traces,
        )

    @staticmethod
    def _check_cancelled(cancelled: CancellationCheck) -> None:
        if cancelled():
            raise RuntimeError("native research run cancelled before publication")


def _lab_plugin_version(spec: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return only an immutable Lab plugin version; never inspect executable source here."""
    version = spec.get("lab_strategy_version")
    if version is None:
        return None
    if not isinstance(version, Mapping):
        raise ValueError("lab_strategy_version must be an object")
    content = version.get("content")
    if not isinstance(content, Mapping):
        raise ValueError("lab_strategy_version.content must be an object")
    if str(content.get("mode")) != "plugin":
        return None
    if spec.get("mode") != "lab_strategy_version":
        raise ValueError(
            "plugin execution is supported only through immutable Lab strategy versions"
        )
    return version


def _is_btcqt_s2(spec: Mapping[str, object]) -> bool:
    version = spec.get("lab_strategy_version")
    if not isinstance(version, Mapping):
        return False
    content = version.get("content")
    return isinstance(content, Mapping) and content.get("builtin_identity") == "btcqt:btcqt_s2_crowding_fader"


def _causal_records(frame: pd.DataFrame) -> tuple[BtcqtCausalRecord, ...]:
    """Preserve observed and available timestamps for source indicator ingestion."""
    records: list[BtcqtCausalRecord] = []
    for observed_at, row in frame.iterrows():
        available_at = row["available_at"]
        if not isinstance(available_at, pd.Timestamp):
            available_at = pd.Timestamp(available_at)
        values = {
            str(column): value.item() if hasattr(value, "item") else value
            for column, value in row.items()
            if column != "available_at"
        }
        records.append(
            BtcqtCausalRecord(
                observed_at=observed_at.to_pydatetime(),
                available_at=available_at.to_pydatetime(),
                values=values,
            ),
        )
    return tuple(records)


def _native_strategy(candidate: object) -> Strategy:
    """Accept the immutable factory envelope without replacing its strategy."""
    if isinstance(candidate, Strategy):
        return candidate
    strategy = getattr(candidate, "strategy", None)
    if isinstance(strategy, Strategy):
        return strategy
    raise TypeError("strategy factory did not provide a btc_backtest Strategy")


def _embedded_version_resolver(
    spec: Mapping[str, object],
) -> Callable[[str], Mapping[str, object]] | None:
    """Resolve only the graph frozen into a durable Lab job.

    A worker never rereads mutable Lab rows after enqueue. The Lab service
    hydrates each referenced child into ``strategy_version`` before persisting
    the job; this resolver traverses that immutable payload by version id.
    """

    root = spec.get("lab_strategy_version")
    if not isinstance(root, Mapping):
        return None
    versions: dict[str, Mapping[str, object]] = {}

    def collect(version: Mapping[str, object]) -> None:
        version_id = version.get("version_id")
        content = version.get("content")
        if not isinstance(version_id, str) or not isinstance(content, Mapping):
            raise ValueError("lab strategy graph contains an invalid version")
        prior = versions.get(version_id)
        if prior is not None and prior != version:
            raise ValueError(f"lab strategy graph has conflicting version {version_id}")
        if prior is not None:
            return
        versions[version_id] = version
        mode = content.get("mode")
        children = content.get("ensemble" if mode == "ensemble" else "regimes")
        if children is None:
            return
        if not isinstance(children, list):
            raise ValueError("lab strategy graph children must be a list")
        for child in children:
            if not isinstance(child, Mapping):
                raise ValueError("lab strategy graph child must be an object")
            embedded = child.get("strategy_version")
            if isinstance(embedded, Mapping):
                collect(embedded)

    collect(root)

    def resolve(version_id: str) -> Mapping[str, object]:
        try:
            return versions[version_id]
        except KeyError as error:
            raise ValueError(
                f"immutable Lab job does not contain referenced child version {version_id}"
            ) from error

    return resolve


def _assumptions(spec: Mapping[str, object]) -> ExecutionAssumptions:
    raw = spec.get("assumptions", spec.get("costs", {}))
    if not isinstance(raw, Mapping):
        raise ValueError("assumptions must be an object")
    allowed = {
        "initial_cash",
        "fee_bps",
        "maker_fee",
        "taker_fee",
        "maker_fee_bps",
        "taker_fee_bps",
        "slippage_bps",
        "spread_bps",
        "borrow_bps",
        "one_tick_slippage",
        "slippage_probability",
        "limit_fill_probability",
        "latency_nanos",
        "queue_position",
        "liquidity_consumption",
        "leverage",
        "maintenance_margin",
        "liquidation_trigger_ratio",
        "funding_bps",
    }
    unsupported = sorted(str(key) for key in set(raw).difference(allowed))
    if unsupported:
        raise ValueError(f"unsupported native cost assumptions: {', '.join(unsupported)}")
    for unavailable in ("spread_bps", "borrow_bps"):
        if Decimal(str(raw.get(unavailable, 0))) != 0:
            raise ValueError(
                f"{unavailable} requires bid/ask or borrow-rate data and is unavailable for OHLCV bars",
            )
    fee_bps = Decimal(str(raw.get("fee_bps", 10))) / Decimal("10000")
    maker_fee = Decimal(str(raw.get("maker_fee", fee_bps)))
    taker_fee = Decimal(str(raw.get("taker_fee", fee_bps)))
    if "maker_fee_bps" in raw:
        maker_fee = Decimal(str(raw["maker_fee_bps"])) / Decimal("10000")
    if "taker_fee_bps" in raw:
        taker_fee = Decimal(str(raw["taker_fee_bps"])) / Decimal("10000")
    return ExecutionAssumptions(
        initial_cash=Decimal(str(raw.get("initial_cash", 10_000))),
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        slippage_bps=Decimal(str(raw.get("slippage_bps", 0))),
        one_tick_slippage=bool(raw.get("one_tick_slippage", False)),
        limit_fill_probability=float(raw.get("limit_fill_probability", 1)),
        slippage_probability=float(
            raw.get("slippage_probability", 1 if raw.get("one_tick_slippage", False) else 0)
        ),
        latency_nanos=int(raw.get("latency_nanos", 0)),
        seed=int(spec.get("seed", 7)),
        queue_position=bool(raw.get("queue_position", False)),
        liquidity_consumption=bool(raw.get("liquidity_consumption", False)),
        leverage=Decimal(str(raw.get("leverage", 1))),
        maintenance_margin=Decimal(str(raw.get("maintenance_margin", "0.05"))),
        liquidation_trigger_ratio=Decimal(str(raw.get("liquidation_trigger_ratio", 1))),
        funding_bps=Decimal(str(raw.get("funding_bps", 0))),
    )


def _execution_window(
    frame: pd.DataFrame,
    spec: Mapping[str, object],
) -> tuple[pd.DataFrame, pd.Timestamp | None]:
    """Keep observed warmup bars, but prevent pre-window order decisions."""
    start = _timestamp(spec.get("from"), "from")
    end = _timestamp(spec.get("to"), "to")
    if start is not None and end is not None and start >= end:
        raise ValueError("from must be earlier than to")
    selected = frame.loc[:end] if end is not None else frame
    if selected.empty:
        raise ValueError("requested execution window contains no bars")
    if start is not None and start > selected.index[-1]:
        raise ValueError("requested execution window starts after available data")
    return selected, start


def _timestamp(value: object, name: str) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from error
    if timestamp.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return timestamp.tz_convert(timezone.utc)


def _initializer(
    strategy: object,
    spec: Mapping[str, object],
    dataset: Mapping[str, object],
    frame: pd.DataFrame,
    assumptions: ExecutionAssumptions,
) -> Callable[[], None]:
    start = frame.index[0].to_pydatetime()
    interval = frame.index[1] - frame.index[0] if len(frame) > 1 else timedelta(minutes=1)
    request = DataRequest(
        provider=str(dataset["provider"]),
        symbol=str(dataset["symbol"]),
        timeframe=str(dataset["timeframe"]),
        start=start,
        end=(frame.index[-1] + interval).to_pydatetime(),
        market=str(dataset["market"]),
    )
    legacy_spec = BacktestSpec(
        strategy=str(strategy.metadata.id),
        strategy_params=dict(spec.get("strategy_params", {})),
        data=request,
        initial_cash=assumptions.initial_cash,
        fee_bps=assumptions.taker_fee * Decimal("10000"),
        slippage_bps=assumptions.slippage_bps,
        seed=assumptions.seed,
    )
    context = InitializationContext(
        spec=legacy_spec, data_manifests=(), parameters=legacy_spec.strategy_params
    )
    return lambda: strategy.initialize(context)


def _instrument(
    dataset: Mapping[str, object],
    spec: Mapping[str, object],
    *,
    strategy: Strategy | None = None,
) -> InstrumentDefinition:
    symbol = str(dataset["symbol"])
    if symbol.count("/") != 1:
        raise ValueError("dataset symbol must be explicit BASE/QUOTE identity")
    base_currency, quote_currency = symbol.split("/", maxsplit=1)
    if not base_currency or not quote_currency:
        raise ValueError("dataset symbol must contain non-empty BASE and QUOTE currencies")
    dataset_market = str(dataset.get("market", "spot"))
    requested_market = spec.get("market")
    if requested_market is not None and str(requested_market) != dataset_market:
        raise ValueError(
            "requested market does not match selected dataset market; choose matching data identity",
        )
    if dataset_market not in {"spot", "perpetual"}:
        raise ValueError(f"unsupported dataset market identity: {dataset_market}")
    required_kind = getattr(strategy, "native_instrument_kind", None)
    if required_kind is not None and str(getattr(required_kind, "value", required_kind)) != dataset_market:
        raise ValueError(
            "source strategy requires a perpetual dataset identity; "
            "spot candles cannot be relabeled as Binance USDT-M execution",
        )
    venue = str(dataset.get("venue", dataset["provider"])).upper().replace("-", "")
    return InstrumentDefinition(
        symbol=f"{base_currency}{quote_currency}",
        venue=venue,
        base_currency=base_currency,
        quote_currency=quote_currency,
        kind="perpetual" if dataset_market == "perpetual" else "spot",
    )


def _run_id(spec: Mapping[str, object], dataset: Mapping[str, object], engine_version: str) -> str:
    encoded = json.dumps(
        {
            "spec": dict(spec),
            "fingerprint": dataset.get("fingerprint"),
            "engine_version": engine_version,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()[:24]


def _write_frame(directory: Path, name: str, frame: pd.DataFrame) -> NautilusArtifact:
    target = directory / name
    _atomic_write(target, frame.to_csv(index=False))
    return _artifact(target, "text/csv", len(frame))


def _write_json(directory: Path, name: str, value: object) -> NautilusArtifact:
    target = directory / name
    _atomic_write(target, json.dumps(value, sort_keys=True, default=str) + "\n")
    return _artifact(target, "application/json", None)


def _atomic_write(target: Path, content: str) -> None:
    if target.exists():
        if target.read_text(encoding="utf-8") != content:
            raise FileExistsError(
                f"refusing to overwrite immutable native artifact with different content: {target}"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _artifact(path: Path, media_type: str, rows: int | None) -> NautilusArtifact:
    return NautilusArtifact(
        name=path.name,
        path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        media_type=media_type,
        size_bytes=path.stat().st_size,
        rows=rows,
    )


def _result_payload(
    summary: NautilusRunSummary,
    native: object,
    dataset: Mapping[str, object] | None = None,
    spec: Mapping[str, object] | None = None,
) -> JsonDict:
    # The plugin bootstrap called this helper before data identity was added to
    # the native result contract.  Keep that private call compatible while its
    # owner updates the bootstrap.
    dataset = dataset or {}
    spec = spec or {}
    equity = _equity_series(native.traces, native=native)
    undefined: dict[str, str] = {}
    metrics = _json_contract_value(native.metrics, path="native", undefined=undefined)
    if not isinstance(metrics, dict):
        raise RuntimeError("native metrics must be an object")
    canonical = _canonical_metrics(equity)
    canonical_undefined = canonical.pop("undefined_reasons", {})
    metrics.update(canonical)
    if undefined or canonical_undefined:
        metrics["undefined_reasons"] = {**undefined, **canonical_undefined}
    returns = _equity_returns(equity)
    # Realized commissions are outcomes and can differ between otherwise
    # comparable walk-forward folds.  The top-level costs field therefore
    # identifies the immutable execution cost model; realized native amounts
    # remain explicitly available without being mistaken for an assumption.
    cost_model = _cost_model(spec)
    realized_costs = dict(metrics.get("costs", {}))
    market = str(dataset.get("market", "unknown"))
    symbol = dataset.get("symbol")
    window = {"from": spec.get("from"), "to": spec.get("to")}
    benchmark = spec.get(
        "benchmark",
        {"status": "not_run", "reason": "no benchmark was requested"},
    )
    if benchmark in (None, "", [], {}):
        raise ValueError("benchmark must be an explicit non-empty comparison condition")
    data = {
        # These bounds identify the immutable source, rather than a single
        # validation fold.  The selected fold is retained in comparability.window.
        "fingerprint": dataset.get("fingerprint"),
        "symbol": symbol,
        "timeframe": dataset.get("timeframe"),
        "start": dataset.get("start"),
        "end": dataset.get("end"),
        "market": market,
        "venue": dataset.get("venue", dataset.get("provider")),
    }
    configuration = {
        "dataset_fingerprint": dataset.get("fingerprint"),
        "symbol": symbol,
        "timeframe": dataset.get("timeframe"),
        "cashflow": spec.get("cashflow", {"mode": "none"}),
        "risk_budget": {
            "initial_cash": str(_initial_cash(spec)),
            "leverage": cost_model["leverage"],
        },
        "execution_model": {
            "engine": summary.engine,
            "version": summary.engine_version,
            "data_requirement": summary.data_requirement.value,
            "bar_timestamp_semantics": "event_open_init_close",
            "liquidate_at_end": False,
        },
        "benchmark": benchmark,
        "cost_model": cost_model,
    }
    return {
        "schema_version": "3",
        "run_id": summary.run_id,
        "engine": summary.engine,
        "engine_version": summary.engine_version,
        "strategy": summary.strategy_id,
        "instrument": {
            "symbol": symbol,
            "venue": dataset.get("venue", dataset.get("provider")),
            "market": market,
            "quote_currency": str(symbol).split("/")[-1] if symbol else None,
            "price_precision": dataset.get("price_precision"),
            "size_precision": dataset.get("size_precision"),
        },
        "benchmark": benchmark,
        "data": data,
        "configuration": configuration,
        "costs": cost_model,
        "realized_costs": realized_costs,
        "account_invariants": dict(native.account_invariants),
        "comparability": {
            "engine": summary.engine,
            "engine_version": summary.engine_version,
            "dataset_id": dataset.get("id", dataset.get("dataset_id")),
            "dataset_fingerprint": dataset.get("fingerprint"),
            "window": window,
            "market": market,
            "initial_cash": str(_initial_cash(spec)),
            "cost_model": cost_model,
            "liquidate_at_end": False,
        },
        "metrics": metrics,
        "series": {
            "returns": returns,
            "equity": equity,
            "monthly_returns": _monthly_returns(equity),
        },
        "decision_traces": _decision_traces(summary.traces),
        "artifacts": [
            {
                "name": artifact.name,
                "path": str(artifact.path),
                "sha256": artifact.sha256,
                "media_type": artifact.media_type,
                "size_bytes": artifact.size_bytes,
                "rows": artifact.rows,
            }
            for artifact in summary.reports
        ],
    }


def _equity_series(
    traces: tuple[object, ...],
    *,
    native: object | None = None,
) -> dict[str, float]:
    result: dict[str, float] = {}
    if native is not None:
        result[native.initial_timestamp.isoformat()] = float(native.initial_equity)
    for trace in traces:
        values = dict(trace.observed_values)
        value = values.get("native_equity")
        if value is None:
            continue
        result[trace.timestamp.isoformat()] = float(Decimal(value))
    if native is not None:
        # Result observations are post-engine account values; DecisionTrace
        # remains the causal, pre-submission record for its bar.
        result[native.terminal_timestamp.isoformat()] = float(native.terminal_equity)
    return result


def _decision_traces(traces: tuple[object, ...]) -> list[JsonDict]:
    return [
        {
            "timestamp": trace.timestamp.isoformat(),
            "strategy_id": trace.strategy_id,
            "observed_rows": trace.observed_rows,
            "intent_count": trace.intent_count,
            "reasons": list(trace.reasons),
            "no_trade_cause": trace.no_trade_cause,
            "observed_values": dict(trace.observed_values),
        }
        for trace in traces
    ]


def _canonical_metrics(equity: Mapping[str, float]) -> JsonDict:
    """Annualized metrics from native portfolio marked-to-market equity only."""
    values = list(equity.values())
    if not values:
        raise RuntimeError("native execution produced no portfolio equity observations")
    initial = values[0]
    terminal = values[-1]
    if initial <= 0:
        raise RuntimeError("native portfolio equity must be positive")
    timestamps = [pd.Timestamp(timestamp) for timestamp in equity]
    intervals = [
        (later - earlier).total_seconds()
        for earlier, later in pairwise(timestamps)
        if later > earlier
    ]
    if not intervals:
        raise RuntimeError("native equity observations require at least two increasing timestamps")
    interval_seconds = sorted(intervals)[len(intervals) // 2]
    annualization_periods = 365.25 * 24 * 60 * 60 / interval_seconds
    returns = [later / earlier - 1 for earlier, later in pairwise(values) if earlier > 0]
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    net_return = terminal / initial - 1
    sharpe: float | None = None
    sharpe_reason: str | None = "fewer than two account return observations"
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        if variance > 0:
            sharpe = mean / sqrt(variance) * sqrt(annualization_periods)
            sharpe_reason = None
        else:
            sharpe_reason = "zero return variance"
    elapsed_seconds = (timestamps[-1] - timestamps[0]).total_seconds()
    if elapsed_seconds <= 0:
        raise RuntimeError("native equity observations require a positive elapsed duration")
    cagr = (terminal / initial) ** (365.25 * 24 * 60 * 60 / elapsed_seconds) - 1
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 else None
    return {
        "net_return": net_return,
        "total_return": net_return,
        "return_pct": net_return * 100,
        "sharpe": sharpe,
        "undefined_reasons": (
            {} if sharpe_reason is None else {"sharpe": sharpe_reason}
        ),
        "calmar": calmar,
        "cagr": cagr,
        "max_drawdown": max_drawdown,
        "terminal_equity": terminal,
        "liquidate_at_end": False,
        "terminal_equity_type": "mark_to_market_unrealized",
        "equity_observations": len(values),
        "annualization_periods": annualization_periods,
        "returns_source": "native_portfolio_mark_to_market",
    }


def _equity_returns(equity: Mapping[str, float]) -> dict[str, float]:
    prior: float | None = None
    result: dict[str, float] = {}
    for timestamp, value in equity.items():
        if prior is not None:
            if prior <= 0:
                raise RuntimeError("native equity must remain positive for return construction")
            result[timestamp] = value / prior - 1
        prior = value
    return result


def _monthly_returns(equity: Mapping[str, float]) -> dict[str, float]:
    month_ends: dict[str, float] = {}
    for timestamp, value in equity.items():
        month = str(pd.Timestamp(timestamp).tz_localize(None).to_period("M"))
        month_ends[month] = value
    prior = next(iter(equity.values()), None)
    if prior is None or prior <= 0:
        return {}
    result: dict[str, float] = {}
    for month, terminal in month_ends.items():
        result[month] = terminal / prior - 1
        prior = terminal
    return result


def _initial_cash(spec: Mapping[str, object]) -> Decimal:
    raw = spec.get("assumptions", spec.get("costs", {}))
    if not isinstance(raw, Mapping):
        return Decimal("10000")
    return Decimal(str(raw.get("initial_cash", 10_000)))


def _cost_model(spec: Mapping[str, object]) -> JsonDict:
    assumptions = _assumptions(spec)
    return {
        "maker_fee": str(assumptions.maker_fee),
        "taker_fee": str(assumptions.taker_fee),
        "slippage_bps": str(assumptions.slippage_bps),
        "one_tick_slippage": assumptions.one_tick_slippage,
        "slippage_probability": assumptions.slippage_probability,
        "limit_fill_probability": assumptions.limit_fill_probability,
        "latency_nanos": assumptions.latency_nanos,
        "queue_position": assumptions.queue_position,
        "liquidity_consumption": assumptions.liquidity_consumption,
        "leverage": str(assumptions.leverage),
        "maintenance_margin": str(assumptions.maintenance_margin),
        "liquidation_trigger_ratio": str(assumptions.liquidation_trigger_ratio),
        "funding_bps": str(assumptions.funding_bps),
    }


def _json_contract_value(
    value: object,
    *,
    path: str,
    undefined: dict[str, str],
) -> object:
    """Make native report values strict-JSON safe without inventing statistics."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        numeric = float(value)
        if isfinite(numeric):
            return numeric
        undefined[path] = "native statistic is non-finite"
        return None
    if isinstance(value, Mapping):
        return {
            str(key): _json_contract_value(
                item,
                path=f"{path}.{key}",
                undefined=undefined,
            )
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [
            _json_contract_value(item, path=f"{path}[{index}]", undefined=undefined)
            for index, item in enumerate(value)
        ]
    return str(value)
