"""Application service and worker handlers for the durable research lab."""
# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
from inspect import Parameter, getdoc, isfunction, signature
from math import isfinite
from pathlib import Path
from typing import Protocol, TypeAlias, cast

import pandas as pd

from qt.lab.persistence import JsonDict, LabRepository
from qt.lab.schemas import (
    CompareRequest,
    CreateStrategyRequest,
    CreateVersionRequest,
    NoteCreateRequest,
    OptimizationRequest,
    RuleGroup,
    StrategyContent,
    SustainedRule,
    ValidateStrategyRequest,
    ValidationRequest,
)
from qt.research.analysis import deflated_sharpe_details
from qt.research.datasets import DatasetCatalog
from qt.research.repository import ResearchRepository

ProgressCallback: TypeAlias = Callable[[str, int], None]
CancellationCheck: TypeAlias = Callable[[], bool]


class InvalidObjectiveError(ValueError):
    """A completed trial did not yield a finite, comparable objective."""


class LabExecutionAdapter(Protocol):
    """Worker-only lab boundary implemented by the native research executor.

    ``spec`` has the normal top-level experiment fields required by
    ``NautilusResearchExecutor`` plus ``lab_execution_kind``,
    ``lab_strategy_version`` and optional ``parameter_overrides``. The native
    adapter must convert immutable rule/plugin versions to a causal strategy.
    """

    def execute(
        self,
        spec: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict: ...


class IsolatedPluginRuntime(Protocol):
    """Required worker-side interface for a plugin version; never used by the API process."""

    def execute_plugin(
        self,
        strategy_version: Mapping[str, object],
        experiment: Mapping[str, object],
        parameter_overrides: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict: ...


class NativeResultPublisher(Protocol):
    """Worker-side publisher that removes local artifact paths before result persistence."""

    def publish_result(self, result: Mapping[str, object]) -> JsonDict: ...


class UnifiedCatalogProvider(Protocol):
    """The single source of real builtin and indicator documentation."""

    def list_builtins(self) -> list[JsonDict]: ...

    def builtin(self, identity: str) -> JsonDict: ...

    def indicator_documentation(self, indicator_id: str) -> JsonDict: ...


class QtUnifiedCatalogProvider:
    """Adapter over QT's registry and preserved 100-profile source catalog."""

    def list_builtins(self) -> list[JsonDict]:
        from btc_backtest.strategies.registry import default_strategy_registry

        from qt.workbench.catalog import legacy_catalog

        registry = default_strategy_registry()
        rows: list[JsonDict] = []
        for strategy_id in registry.list():
            metadata = registry.describe(strategy_id)
            rows.append(
                {
                    "id": f"qt:{strategy_id}",
                    "identity": strategy_id,
                    "version": metadata.version,
                    "description": metadata.description,
                    "parameter_schema": dict(metadata.parameter_schema),
                    "source": "qt.strategy_registry",
                    "read_only": True,
                }
            )
        profiles = legacy_catalog()["profiles"]
        assert isinstance(profiles, list)
        for profile in profiles:
            if isinstance(profile, dict) and isinstance(profile.get("id"), str):
                rows.append(
                    {
                        "id": f"catalog:{profile['id']}",
                        "identity": profile["id"],
                        "version": str(profile.get("version", 1)),
                        "description": f"Preserved catalog profile {profile['id']}",
                        "parameter_schema": {"signal_params": profile.get("signal_params", {})},
                        "source": "btc-quant.catalog_profiles",
                        "read_only": True,
                    }
                )
        return rows

    def builtin(self, identity: str) -> JsonDict:
        for row in self.list_builtins():
            if row["id"] == identity:
                return row
        raise KeyError(identity)

    def indicator_documentation(self, indicator_id: str) -> JsonDict:
        from qt.lab.learning import catalog_signal_documentation
        from qt.workbench.catalog import legacy_catalog

        signals = legacy_catalog()["signals"]
        assert isinstance(signals, list)
        for signal in signals:
            if isinstance(signal, dict) and signal.get("id") == indicator_id:
                return catalog_signal_documentation(indicator_id)
        documentation = _qt_indicator_documentation()
        try:
            return documentation[indicator_id]
        except KeyError as error:
            raise KeyError(indicator_id) from error


def _qt_indicator_documentation() -> dict[str, JsonDict]:
    """Read the actual local indicator definitions rather than maintaining a copy."""

    module_names = (
        "qt.indicators.price",
        "qt.indicators.derivatives",
        "qt.indicators.events",
        "qt.indicators.onchain",
        "qt.indicators.options",
        "qt.indicators.regime",
        "qt.indicators.sentiment",
        "qt.indicators.smartmoney",
        "qt.indicators.talib_standard",
        "qt.indicators.volatility",
    )
    documents: dict[str, JsonDict] = {
        "sma": {
            "id": "sma",
            "kind": "indicator",
            "formula": "close.rolling(window).mean()",
            "parameters": [
                {"name": "window", "required": False, "default": 20, "type": "int"}
            ],
            **_market_bar_availability("price"),
            "source": "qt.workbench.strategy_factory.build_lab_strategy",
        }
    }
    aliases: dict[str, JsonDict] = {}
    for module_name in module_names:
        module = import_module(module_name)
        prefix = module_name.rsplit(".", maxsplit=1)[-1]
        for name, function in vars(module).items():
            if name.startswith("_") or not isfunction(function) or function.__module__ != module_name:
                continue
            parameters: list[JsonDict] = []
            for parameter in signature(function).parameters.values():
                if parameter.kind in {Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD}:
                    continue
                parameters.append(
                    {
                        "name": parameter.name,
                        "required": parameter.default is Parameter.empty,
                        "default": None if parameter.default is Parameter.empty else parameter.default,
                        "type": str(parameter.annotation)
                        if parameter.annotation is not Parameter.empty
                        else None,
                    }
                )
            docstring = getdoc(function)
            document: JsonDict = {
                "id": f"{prefix}.{name}",
                "kind": "indicator",
                "formula": docstring.splitlines()[0] if docstring else None,
                "description": docstring,
                "parameters": parameters,
                **_indicator_availability(prefix),
                "source": f"{module_name}.{name}",
            }
            documents[str(document["id"])] = document
            aliases.setdefault(name, document)
    # The executable lab AST deliberately resolves RSI/ATR through this implementation.
    for name in ("rsi", "atr", "adx"):
        talib_document = documents.get(f"talib_standard.{name}")
        if talib_document is not None:
            aliases[name] = {**talib_document, "id": name}
    documents.update(aliases)
    return documents


def _indicator_availability(family: str) -> JsonDict:
    """Describe availability from source provenance, not an assumed candle close."""

    if family in {"price", "talib_standard", "volatility", "regime", "events"}:
        return _market_bar_availability(family)
    if family in {"onchain", "options", "derivatives", "sentiment", "smartmoney"}:
        return {
            "source_family": family,
            "available_at": "provider-recorded available_at required; may be revised",
            "availability_basis": "provider_recorded_available_at",
            "requires_provider_available_at": True,
            "may_be_revised": True,
        }
    raise ValueError(f"unsupported indicator source family: {family}")


def _market_bar_availability(family: str) -> JsonDict:
    return {
        "source_family": family,
        "available_at": "derived after the completed market bar",
        "availability_basis": "completed_market_bar",
        "requires_provider_available_at": False,
        "may_be_revised": False,
    }


class DatasetTimelineProvider(Protocol):
    """Read-only source of actual completed-bar timestamps for fold construction."""

    def timestamps(self, dataset_id: str) -> pd.DatetimeIndex: ...


class ParquetTimelineProvider:
    """Use the shared parquet catalog; it does not fabricate timestamps or metrics."""

    def __init__(self, parquet_root: Path) -> None:
        self.catalog = DatasetCatalog(parquet_root)

    def timestamps(self, dataset_id: str) -> pd.DatetimeIndex:
        frame = pd.read_parquet(self.catalog.path_for(dataset_id))
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise ValueError("research parquet requires a DatetimeIndex")
        index = (
            frame.index.tz_localize("UTC")
            if frame.index.tz is None
            else frame.index.tz_convert("UTC")
        )
        if not index.is_monotonic_increasing or index.has_duplicates:
            raise ValueError("dataset timestamps must be ordered and unique")
        return index


@dataclass(frozen=True)
class ValidationFold:
    number: int
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    test_start: str
    test_end: str
    purge_bars: int
    embargo_bars: int


class CategoricalTrial(Protocol):
    number: int

    def suggest_categorical(
        self,
        name: str,
        choices: Sequence[float | int | str | bool],
    ) -> float | int | str | bool: ...


class OptunaSamplers(Protocol):
    def GridSampler(  # noqa: N802
        self, search_space: Mapping[str, list[float | int | str | bool]], *, seed: int
    ) -> object: ...

    def RandomSampler(self, *, seed: int) -> object: ...  # noqa: N802

    def TPESampler(self, *, seed: int) -> object: ...  # noqa: N802


class OptunaModule(Protocol):
    samplers: OptunaSamplers


class LabService:
    def __init__(
        self,
        repository: LabRepository,
        executor: LabExecutionAdapter | None = None,
        catalog: UnifiedCatalogProvider | None = None,
        timeline_provider: DatasetTimelineProvider | None = None,
        research_repository: ResearchRepository | None = None,
        result_publisher: NativeResultPublisher | None = None,
    ) -> None:
        self.repository = repository
        self.research_repository = research_repository or ResearchRepository(repository.path)
        self.executor = executor
        self.catalog = catalog or QtUnifiedCatalogProvider()
        self.timeline_provider = timeline_provider
        self.result_publisher = result_publisher

    def create_strategy(self, request: CreateStrategyRequest) -> JsonDict:
        builtin = None
        if request.clone_builtin_id:
            try:
                builtin = self.catalog.builtin(request.clone_builtin_id)
            except KeyError as error:
                raise ValueError("unknown built-in strategy") from error
        strategy = self.repository.create_strategy(
            request.name, request.description, request.clone_builtin_id
        )
        content = request.content
        if content is not None:
            self._validate_content(content)
        if content is None and builtin is not None:
            content = StrategyContent.model_validate(
                {
                    "mode": "builtin",
                    "builtin_identity": builtin["id"],
                    "builtin_version": builtin["version"],
                }
            )
        version = None
        if content is not None:
            version = self.repository.create_version(
                str(strategy["strategy_id"]), 0, content.model_dump(mode="json"), "initial draft"
            )
        return {
            "strategy": strategy,
            "version": version,
            "clone_source": builtin["id"] if builtin else None,
        }

    def create_version(self, strategy_id: str, request: CreateVersionRequest) -> JsonDict:
        self._validate_content(request.content)
        return self.repository.create_version(
            strategy_id,
            request.expected_revision,
            request.content.model_dump(mode="json"),
            request.message,
        )

    def validate(self, request: ValidateStrategyRequest) -> JsonDict:
        self._validate_content(request.content)
        return {
            "valid": True,
            "explainers": _explain_content(request.content, request.sample_values),
            "plugin_execution": "worker_isolation_required"
            if request.content.mode == "plugin"
            else "not_applicable",
        }

    def strategy_documentation(self, strategy_id: str) -> JsonDict:
        return self.catalog.builtin(strategy_id)

    def indicator_documentation(self, indicator_id: str) -> JsonDict:
        return self.catalog.indicator_documentation(indicator_id)

    def learning_documentation(self) -> list[JsonDict]:
        from qt.lab.learning import (
            list_catalog_signal_documentation,
            list_indicator_family_documentation,
        )

        concrete = [
            document
            for identifier, document in _qt_indicator_documentation().items()
            if identifier == "sma" or "." in identifier
        ]
        return [
            *list_catalog_signal_documentation(),
            *list_indicator_family_documentation(),
            *sorted(concrete, key=lambda document: str(document["id"])),
        ]

    def execution_version(self, version_id: str) -> JsonDict:
        """Return the bounded, immutable execution graph for a persisted version."""

        return _hydrate_execution_version(
            self.repository.get_version(version_id), self.repository.get_version
        )

    def create_note(self, request: NoteCreateRequest) -> JsonDict:
        assert request.strategy_version_id is not None
        self.repository.get_version(request.strategy_version_id)
        return self.repository.create_note(request.model_dump(mode="json"))

    def compare(self, request: CompareRequest) -> JsonDict:
        results = [self.repository.get_result(result_id) for result_id in request.result_ids]
        eligible = [
            result
            for result in results
            if not _temporal_integrity_reasons(result.get("summary"))
        ]
        baseline = (eligible[0] if eligible else results[0])["comparability_group"]
        baseline_missing = _missing_comparability_fields(baseline)
        comparable = (
            []
            if baseline_missing
            else [
                result["result_id"]
                for result in eligible
                if result["comparability_group"] == baseline
                and not _missing_comparability_fields(result["comparability_group"])
            ]
        )
        incompatible = [
            {
                "result_id": result["result_id"],
                "reasons": (
                    _temporal_integrity_reasons(result.get("summary"))
                    + _comparability_reasons(baseline, result["comparability_group"])
                ),
            }
            for result in results
            if result["result_id"] not in comparable
        ]
        metrics: list[JsonDict] = []
        series: dict[str, JsonDict] = {}
        monthly: dict[str, object] = {}
        costs: dict[str, object] = {}
        for result in results:
            summary = result["summary"]
            raw_metrics = summary.get("metrics") if isinstance(summary, Mapping) else None
            metrics.append(
                {
                    "result_id": result["result_id"],
                    "metrics": dict(raw_metrics) if isinstance(raw_metrics, Mapping) else None,
                    "temporal_integrity": (
                        dict(summary["temporal_integrity"])
                        if isinstance(summary, Mapping)
                        and isinstance(summary.get("temporal_integrity"), Mapping)
                        else None
                    ),
                }
            )
            if result["result_id"] in comparable and isinstance(summary, Mapping):
                bounded = _bounded_series(summary.get("series"), request.max_points)
                if bounded is not None:
                    series[str(result["result_id"])] = bounded
                raw_monthly = summary.get("monthly_returns")
                if not isinstance(raw_monthly, Mapping):
                    raw_series = summary.get("series")
                    raw_monthly = (
                        raw_series.get("monthly_returns")
                        if isinstance(raw_series, Mapping)
                        else None
                    )
                if isinstance(raw_monthly, Mapping):
                    monthly[str(result["result_id"])] = dict(raw_monthly)
                raw_costs = summary.get("costs")
                if isinstance(raw_costs, Mapping):
                    costs[str(result["result_id"])] = dict(raw_costs)
        return {
            "comparability_group": baseline,
            "comparable_result_ids": comparable,
            "incomparable": incompatible,
            "metrics": metrics,
            "series": series if comparable else None,
            "monthly_returns": monthly if monthly else None,
            "costs": costs if costs else None,
            "correlation": _correlation(series) if len(series) >= 2 else None,
        }

    def enqueue_optimization(
        self,
        request: OptimizationRequest,
        *,
        idempotency_key: str,
    ) -> tuple[JsonDict, bool]:
        version = self.repository.get_version(request.strategy_version_id)
        payload = request.model_dump(mode="json")
        payload["strategy_version"] = version
        self._validate_requested_bindings(version, _object(payload["search_space"], "search_space"))
        return self.research_repository.enqueue_idempotent(
            {"job_type": "lab_optimization", "payload": payload},
            idempotency_key=idempotency_key,
        )

    def enqueue_validation(
        self,
        request: ValidationRequest,
        *,
        idempotency_key: str,
    ) -> tuple[JsonDict, bool]:
        version = self.repository.get_version(request.strategy_version_id)
        payload = request.model_dump(mode="json")
        payload["strategy_version"] = version
        for candidate in _parameter_candidates(payload["candidate_parameters"]):
            self._validate_requested_bindings(version, candidate)
        return self.research_repository.enqueue_idempotent(
            {"job_type": "lab_validation", "payload": payload},
            idempotency_key=idempotency_key,
        )

    def get_job(self, job_id: str) -> JsonDict:
        return self.research_repository.get_job(job_id)

    def request_cancel(self, job_id: str) -> JsonDict:
        return self.research_repository.request_cancel(job_id)

    def import_native_result(
        self,
        result: Mapping[str, object],
        decision_traces: list[Mapping[str, object]] | None = None,
    ) -> JsonDict:
        """Import an actual completed native result for API detail and trace reads."""

        traces = decision_traces
        if traces is None:
            raw_traces = result.get("decision_traces", [])
            traces = (
                [dict(item) for item in raw_traces if isinstance(item, Mapping)]
                if isinstance(raw_traces, list)
                else []
            )
        return self.repository.store_result(_with_temporal_integrity(result), traces)

    def list_results(self, *, limit: int = 50) -> list[JsonDict]:
        """Bounded aggregate input for the lead-owned ``GET /results`` route."""

        return self.repository.list_results(limit=limit)

    def get_optimization(self, job_id: str) -> JsonDict:
        job = self.research_repository.get_job(job_id)
        if _job_type(job) != "lab_optimization":
            raise KeyError(job_id)
        job["trials"] = self.repository.list_trials(job_id)
        return job

    def get_validation(self, job_id: str) -> JsonDict:
        job = self.research_repository.get_job(job_id)
        if _job_type(job) != "lab_validation":
            raise KeyError(job_id)
        return job

    def execute(
        self,
        spec: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict:
        """Execute a claimed shared-queue lab job without claiming or completing it.

        The shared worker adds ``lab_job_id`` to its in-memory dispatch copy so
        immutable trial rows can be associated with the shared job. That field
        is not persisted into ``research_jobs.spec_json``.
        """

        if self.executor is None:
            raise RuntimeError("no native research executor is configured for lab jobs")
        job_type = _text(spec.get("job_type"), "job_type")
        payload = _mapping(spec.get("payload"), "payload")
        if job_type == "lab_optimization":
            job_id = _text(spec.get("lab_job_id"), "lab_job_id")
            return self._run_optimization(job_id, payload, progress, cancelled)
        if job_type == "lab_validation":
            return self._run_validation(payload, progress, cancelled)
        raise ValueError(f"unsupported lab job_type: {job_type}")

    def _run_optimization(
        self,
        job_id: str,
        payload: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict:
        try:
            import optuna
        except ImportError as error:
            raise RuntimeError("Optuna is required to execute optimization jobs") from error
        typed_optuna = cast(OptunaModule, optuna)
        sampler_name = str(payload["sampler"])
        seed = _integer(payload["seed"], "seed")
        space = _object(payload["search_space"], "search_space")
        storage_path = self.repository.path.with_name("lab_optuna.sqlite3")
        sampler = _optuna_sampler(typed_optuna, sampler_name, space, seed)
        study = optuna.create_study(
            study_name=f"lab_{job_id}",
            direction="maximize",
            sampler=sampler,
            storage=f"sqlite:///{storage_path}",
            load_if_exists=True,
        )
        persisted_before = len(study.trials)
        effective_seed = _resume_sampler_seed(seed, job_id, persisted_before)
        if sampler_name != "grid" and persisted_before:
            # Optuna persists trials but not Random/TPE RNG state. Advance to a
            # deterministic new stream keyed by the immutable job and cursor.
            study.sampler = _optuna_sampler(typed_optuna, sampler_name, space, effective_seed)
        budget = _integer(payload["budget"], "budget")
        objective = str(payload["objective"])

        def evaluate(trial: object) -> float:
            typed_trial = cast(CategoricalTrial, trial)
            number = typed_trial.number
            parameters = {
                name: typed_trial.suggest_categorical(name, values)
                for name, values in space.items()
            }
            if cancelled():
                self.repository.add_trial(
                    job_id,
                    number,
                    parameters,
                    "CANCELLED",
                    None,
                    {"reason": "optimization cancellation requested"},
                )
                raise RuntimeError("optimization cancellation requested")
            progress("optimization_trial", min(95, 5 + int(number * 90 / budget)))
            spec = self._native_spec(
                payload,
                execution_kind="optimization_trial",
                parameter_overrides=parameters,
            )
            try:
                assert self.executor is not None
                result = self._execute_native(spec, progress, cancelled)
                value = _objective_value(result, objective)
                self.repository.add_trial(job_id, number, parameters, "COMPLETE", value, result)
                self._store_result_if_present(result)
                return value
            except InvalidObjectiveError as error:
                self.repository.add_trial(
                    job_id,
                    number,
                    parameters,
                    "FAIL",
                    None,
                    {"error": str(error)},
                )
                raise
            except Exception as error:
                self.repository.add_trial(
                    job_id,
                    number,
                    parameters,
                    "CANCELLED" if cancelled() else "FAIL",
                    None,
                    {"error": str(error)},
                )
                raise

        remaining = max(0, budget - len(study.trials))
        study.optimize(evaluate, n_trials=remaining, catch=(InvalidObjectiveError,))
        trials = self.repository.list_trials(job_id)
        completed = [trial for trial in trials if trial["state"] == "COMPLETE"]
        if not completed:
            raise RuntimeError("optimization failed: no trial produced a finite objective value")
        best_trial_number = int(study.best_trial.number)
        return {
            "best_parameters": dict(study.best_params),
            "best_value": float(study.best_value),
            "objective": objective,
            "trials": len(trials),
            "all_trials_persisted": True,
            "statistics": {
                "deflated_sharpe": _optimization_dsr(
                    trials,
                    attempted_variants=len(study.trials),
                    selected_trial_number=best_trial_number,
                )
            },
            "sampler_state": {
                "name": sampler_name,
                "base_seed": seed,
                "persisted_trials_before": persisted_before,
                "effective_seed": effective_seed,
                "resume_policy": "deterministic job-id and persisted-trial cursor",
            },
        }

    def _run_validation(
        self,
        payload: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict:
        assert self.executor is not None
        if payload["profile"] == "quick":
            result = self._execute_native(
                self._native_spec(payload, execution_kind="quick_diagnostic", parameter_overrides={}),
                progress,
                cancelled,
            )
            self._store_result_if_present(result)
            return {
                "result": result,
                "validation_status": "diagnostic_only",
                "reason": "quick profile does not perform walk-forward validation",
            }
        if self.timeline_provider is None:
            raise RuntimeError("standard validation requires a real parquet timeline provider")
        experiment = _mapping(payload["experiment"], "experiment")
        dataset_id = _text(experiment.get("dataset_id"), "experiment.dataset_id")
        folds = build_validation_folds(
            self.timeline_provider.timestamps(dataset_id),
            train_bars=_integer(payload["train_bars"], "train_bars"),
            validation_bars=_integer(payload["validation_bars"], "validation_bars"),
            test_bars=_integer(payload["test_bars"], "test_bars"),
            folds=_integer(payload["folds"], "folds"),
            purge_bars=_integer(payload["purge_bars"], "purge_bars"),
            embargo_bars=_integer(payload["embargo_bars"], "embargo_bars"),
        )
        candidates = _parameter_candidates(payload["candidate_parameters"])
        cscv_requested = bool(payload.get("cscv_diagnostic", False))
        cscv_oos_scores: list[list[float]] = [[] for _ in candidates]
        fold_outputs: list[JsonDict] = []
        oos_returns: list[pd.Series] = []
        for fold in folds:
            if cancelled():
                raise RuntimeError("validation cancellation requested")
            candidate_runs: list[JsonDict] = []
            for index, parameters in enumerate(candidates):
                train = self._execute_native(
                    self._windowed_native_spec(payload, fold, "train", parameters),
                    progress,
                    cancelled,
                )
                validation = self._execute_native(
                    self._windowed_native_spec(payload, fold, "validation", parameters),
                    progress,
                    cancelled,
                )
                candidate_runs.append(
                    {
                        "parameters": parameters,
                        "train": train,
                        "validation": validation,
                        "validation_score": _objective_value(validation, "sharpe"),
                        "candidate_index": index,
                    }
                )
            selected = max(
                candidate_runs,
                key=lambda item: (
                    _number(item["validation_score"], "validation_score"),
                    -_integer(item["candidate_index"], "candidate_index"),
                ),
            )
            test = self._execute_native(
                self._windowed_native_spec(
                    payload, fold, "test", _mapping(selected["parameters"], "parameters")
                ),
                progress,
                cancelled,
            )
            self._store_result_if_present(test)
            returns = _result_returns(test)
            if returns is not None:
                oos_returns.append(returns)
            if cscv_requested:
                for candidate in candidate_runs:
                    candidate_index = _integer(candidate["candidate_index"], "candidate_index")
                    if candidate_index == _integer(selected["candidate_index"], "candidate_index"):
                        cscv_test = test
                    else:
                        cscv_test = self._execute_native(
                            self._windowed_native_spec(
                                payload,
                                fold,
                                "test",
                                _mapping(candidate["parameters"], "parameters"),
                            ),
                            progress,
                            cancelled,
                        )
                    cscv_oos_scores[candidate_index].append(
                        _objective_value(cscv_test, "sharpe")
                    )
            fold_outputs.append(
                {
                    "fold": fold.__dict__,
                    "selected_parameters": selected["parameters"],
                    "train_metrics": _metrics(_mapping(selected["train"], "train result")),
                    "validation_metrics": _metrics(
                        _mapping(selected["validation"], "validation result")
                    ),
                    "candidate_validation_scores": [
                        _number(candidate["validation_score"], "validation_score")
                        for candidate in candidate_runs
                    ],
                    "test_metrics": _metrics(test),
                    "test_result_id": test.get("run_id", test.get("result_id")),
                }
            )
        combined_returns = pd.concat(oos_returns, ignore_index=True) if oos_returns else None
        statistics = _validation_statistics(
            combined_returns,
            candidates,
            fold_outputs,
            cscv_diagnostic=cscv_oos_scores if cscv_requested else None,
            dataset_id=dataset_id,
        )
        return {
            "validation_status": "strict_walk_forward",
            "selection_policy": "each fold selects by validation Sharpe after train and validation only; test is read once after selection",
            "folds": fold_outputs,
            "statistics": statistics,
        }

    def _store_result_if_present(self, result: Mapping[str, object]) -> None:
        if result.get("run_id") or result.get("result_id"):
            self.import_native_result(result)

    def _execute_native(
        self,
        spec: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict:
        assert self.executor is not None
        result = self.executor.execute(spec, progress, cancelled)
        if self.result_publisher is not None:
            result = self.result_publisher.publish_result(result)
        return _with_temporal_integrity(result, spec)

    def _native_spec(
        self,
        payload: Mapping[str, object],
        *,
        execution_kind: str,
        parameter_overrides: Mapping[str, object],
    ) -> JsonDict:
        return _native_spec(
            payload,
            execution_kind=execution_kind,
            parameter_overrides=parameter_overrides,
            version_resolver=self.repository.get_version,
        )

    def _windowed_native_spec(
        self,
        payload: Mapping[str, object],
        fold: ValidationFold,
        phase: str,
        parameter_overrides: Mapping[str, object],
    ) -> JsonDict:
        return _windowed_native_spec(
            payload,
            fold,
            phase,
            parameter_overrides,
            version_resolver=self.repository.get_version,
        )

    def _validate_content(self, content: StrategyContent) -> None:
        self._validate_version_links(content)
        declared = {parameter.name for parameter in content.parameters}
        references = _parameter_references(content.model_dump(mode="json"))
        missing = references.difference(declared)
        if missing:
            raise ValueError(
                "parameter references are not declared: " + ", ".join(sorted(missing))
            )

    def _validate_requested_bindings(
        self, version: Mapping[str, object], values: Mapping[str, object]
    ) -> None:
        content = _mapping(version.get("content"), "strategy_version.content")
        definitions = content.get("parameters", [])
        if not isinstance(definitions, list):
            raise ValueError("strategy_version.content.parameters must be a list")
        declared = {
            str(item["name"])
            for item in definitions
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        references = _parameter_references(content)
        unknown = set(values).difference(declared)
        if unknown:
            raise ValueError("unknown parameter bindings: " + ", ".join(sorted(unknown)))
        unused = set(values).difference(references)
        if unused:
            raise ValueError(
                "parameter bindings must be referenced as ${name} in the strategy: "
                + ", ".join(sorted(unused))
            )

    def _validate_version_links(self, content: StrategyContent) -> None:
        if content.mode == "ensemble":
            if abs(sum(member.target_weight for member in content.ensemble) - 1.0) > 1e-9:
                raise ValueError("ensemble target weights must sum to one")
            for member in content.ensemble:
                self.repository.get_version(member.strategy_version_id)
        if content.mode == "regime_switch":
            names = [branch.name for branch in content.regimes]
            if len(names) != len(set(names)):
                raise ValueError("regime branch names must be unique")
            for branch in content.regimes:
                self.repository.get_version(branch.strategy_version_id)


def _object(value: object, field: str) -> dict[str, list[float | int | str | bool]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    converted: dict[str, list[float | int | str | bool]] = {}
    for key, values in value.items():
        if not isinstance(key, str) or not isinstance(values, list):
            raise ValueError(f"{field} must map names to lists")
        converted[key] = values
    return converted


def _optuna_sampler(
    optuna: OptunaModule,
    sampler_name: str,
    space: Mapping[str, list[float | int | str | bool]],
    seed: int,
) -> object:
    samplers = optuna.samplers
    if sampler_name == "grid":
        return samplers.GridSampler({key: list(values) for key, values in space.items()}, seed=seed)
    if sampler_name == "random":
        return samplers.RandomSampler(seed=seed)
    return samplers.TPESampler(seed=seed)


def _resume_sampler_seed(base_seed: int, job_id: str, persisted_trials: int) -> int:
    if persisted_trials < 0:
        raise ValueError("persisted trial count must not be negative")
    encoded = f"{base_seed}:{job_id}:{persisted_trials}".encode()
    return int.from_bytes(sha256(encoded).digest()[:8], byteorder="big") % (2**32)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _parameter_references(value: object) -> set[str]:
    references: set[str] = set()

    def walk(item: object) -> None:
        if isinstance(item, str) and item.startswith("${") and item.endswith("}"):
            references.add(item[2:-1])
        elif isinstance(item, Mapping):
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return references


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be numeric")
    return float(value)


def _hydrate_execution_version(
    version: Mapping[str, object],
    resolver: Callable[[str], JsonDict] | None,
    *,
    ancestry: frozenset[str] = frozenset(),
    leaf_count: list[int] | None = None,
) -> JsonDict:
    """Attach exact referenced version content for bounded composite execution."""

    version_id = _text(version.get("version_id"), "strategy_version.version_id")
    if version_id in ancestry:
        raise ValueError(f"strategy version graph contains a cycle at {version_id}")
    content = _mapping(version.get("content"), "strategy_version.content")
    mode = _text(content.get("mode"), "strategy_version.content.mode")
    leaves = leaf_count if leaf_count is not None else [0]
    hydrated_content: JsonDict = dict(content)
    next_ancestry = ancestry | {version_id}
    if mode == "ensemble":
        members = content.get("ensemble")
        if not isinstance(members, list):
            raise ValueError("ensemble content must include member versions")
        hydrated_members: list[JsonDict] = []
        for member in members:
            item = _mapping(member, "ensemble member")
            member_id = _text(item.get("strategy_version_id"), "ensemble.strategy_version_id")
            if resolver is None:
                raise ValueError("ensemble execution requires a strategy version resolver")
            hydrated_members.append(
                {
                    **dict(item),
                    "strategy_version": _hydrate_execution_version(
                        resolver(member_id), resolver, ancestry=next_ancestry, leaf_count=leaves
                    ),
                }
            )
        hydrated_content["ensemble"] = hydrated_members
    elif mode == "regime_switch":
        regimes = content.get("regimes")
        if not isinstance(regimes, list):
            raise ValueError("regime_switch content must include branch versions")
        hydrated_regimes: list[JsonDict] = []
        for regime in regimes:
            item = _mapping(regime, "regime branch")
            branch_id = _text(item.get("strategy_version_id"), "regime.strategy_version_id")
            if resolver is None:
                raise ValueError("regime execution requires a strategy version resolver")
            hydrated_regimes.append(
                {
                    **dict(item),
                    "strategy_version": _hydrate_execution_version(
                        resolver(branch_id), resolver, ancestry=next_ancestry, leaf_count=leaves
                    ),
                }
            )
        hydrated_content["regimes"] = hydrated_regimes
    else:
        leaves[0] += 1
        if leaves[0] > 3:
            raise ValueError("composite execution graph may contain at most three leaf strategies")
    return {**dict(version), "content": hydrated_content}


def _native_spec(
    payload: Mapping[str, object],
    *,
    execution_kind: str,
    parameter_overrides: Mapping[str, object],
    version_resolver: Callable[[str], JsonDict] | None = None,
) -> JsonDict:
    experiment = _mapping(payload.get("experiment"), "experiment")
    dataset_id = experiment.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValueError("experiment.dataset_id is required")
    strategy_version = _mapping(payload.get("strategy_version"), "strategy_version")
    execution_version = _hydrate_execution_version(strategy_version, version_resolver)
    native: JsonDict = dict(experiment)
    native.update(
        {
            "dataset_id": dataset_id,
            "seed": payload["seed"],
            "mode": "lab_strategy_version",
            "lab_execution_kind": execution_kind,
            "parameter_overrides": dict(parameter_overrides),
            "lab_parameter_bindings": dict(parameter_overrides),
            "lab_strategy_version": {
                **execution_version,
                "parameter_overrides": dict(parameter_overrides),
            },
        }
    )
    return native


def build_validation_folds(
    timestamps: pd.DatetimeIndex,
    *,
    train_bars: int,
    validation_bars: int,
    test_bars: int,
    folds: int,
    purge_bars: int,
    embargo_bars: int,
) -> list[ValidationFold]:
    """Build chronological non-overlapping OOS folds from completed data only."""

    required = train_bars + purge_bars + validation_bars + embargo_bars + test_bars
    if len(timestamps) < required + (folds - 1) * test_bars:
        raise ValueError("dataset is too short for the requested train/validation/test folds")
    result: list[ValidationFold] = []
    for number in range(folds):
        start = number * test_bars
        train_end = start + train_bars
        validation_start = train_end + purge_bars
        validation_end = validation_start + validation_bars
        test_start = validation_end + embargo_bars
        test_end = test_start + test_bars
        result.append(
            ValidationFold(
                number=number + 1,
                train_start=timestamps[start].isoformat(),
                train_end=timestamps[train_end - 1].isoformat(),
                validation_start=timestamps[validation_start].isoformat(),
                validation_end=timestamps[validation_end - 1].isoformat(),
                test_start=timestamps[test_start].isoformat(),
                test_end=timestamps[test_end - 1].isoformat(),
                purge_bars=purge_bars,
                embargo_bars=embargo_bars,
            )
        )
    return result


def _windowed_native_spec(
    payload: Mapping[str, object],
    fold: ValidationFold,
    phase: str,
    parameter_overrides: Mapping[str, object],
    version_resolver: Callable[[str], JsonDict] | None = None,
) -> JsonDict:
    spec = _native_spec(
        payload,
        execution_kind="walk_forward",
        parameter_overrides=parameter_overrides,
        version_resolver=version_resolver,
    )
    starts = {
        "train": fold.train_start,
        "validation": fold.validation_start,
        "test": fold.test_start,
    }
    ends = {"train": fold.train_end, "validation": fold.validation_end, "test": fold.test_end}
    spec.update(
        {"from": starts[phase], "to": ends[phase], "lab_phase": phase, "lab_fold": fold.number}
    )
    return spec


def _parameter_candidates(value: object) -> list[dict[str, float | int | str | bool]]:
    if not isinstance(value, list):
        raise ValueError("candidate_parameters must be a list")
    candidates: list[dict[str, float | int | str | bool]] = []
    for candidate in value:
        if not isinstance(candidate, Mapping):
            raise ValueError("candidate parameters must be objects")
        converted: dict[str, float | int | str | bool] = {}
        for key, item in candidate.items():
            if not isinstance(key, str) or not isinstance(item, (float, int, str, bool)):
                raise ValueError("candidate parameters must contain scalar values")
            converted[key] = item
        candidates.append(converted)
    return candidates


def _metrics(result: Mapping[str, object]) -> JsonDict:
    raw = result.get("metrics")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _result_returns(result: Mapping[str, object]) -> pd.Series | None:
    series = result.get("series")
    if not isinstance(series, Mapping):
        return None
    values = series.get("returns")
    if not isinstance(values, Mapping):
        return None
    numeric = pd.to_numeric(pd.Series(dict(values)), errors="coerce").dropna()
    return numeric if len(numeric) >= 3 else None


def _optimization_dsr(
    trials: list[JsonDict],
    *,
    attempted_variants: int,
    selected_trial_number: int,
) -> JsonDict:
    """Compute DSR only for comparable, same-window optimizer trial evidence."""

    completed = [
        trial
        for trial in trials
        if trial.get("state") == "COMPLETE" and isinstance(trial.get("result"), Mapping)
    ]
    scope: JsonDict = {
        "attempted_variants": attempted_variants,
        "completed_trials": len(completed),
        "selected_trial_number": selected_trial_number,
        "trial_definition": "same immutable optimizer experiment with parameter bindings",
        "sharpe_scale": "per_bar_nonannualized",
        "returns_source": "native_portfolio_mark_to_market",
        "trial_sharpe_variance_estimator": "sample_variance_ddof_1",
    }
    results = [_mapping(trial["result"], "trial result") for trial in completed]
    integrity_failures = [
        reason
        for result in results
        for reason in _temporal_integrity_reasons(result)
    ]
    if integrity_failures:
        return {
            "status": "not_applicable",
            "reason": "optimizer trial is not verified temporal evidence: " + integrity_failures[0],
            "multiple_testing_scope": scope,
        }
    if not _same_immutable_trial_conditions(results):
        return {
            "status": "not_applicable",
            "reason": "optimizer trial results lack one shared complete immutable comparison group",
            "multiple_testing_scope": scope,
        }
    sharpes: list[float] = []
    selected_returns: pd.Series | None = None
    for trial, result in zip(completed, results, strict=True):
        returns = _result_returns(result)
        if returns is None:
            return {
                "status": "not_applicable",
                "reason": "a completed optimizer trial lacks finite marked-to-market returns",
                "multiple_testing_scope": scope,
            }
        standard_deviation = float(returns.std(ddof=1))
        if standard_deviation <= 0 or not isfinite(standard_deviation):
            return {
                "status": "not_applicable",
                "reason": "a completed optimizer trial has no finite return dispersion",
                "multiple_testing_scope": scope,
            }
        sharpes.append(float(returns.mean() / standard_deviation))
        if trial.get("trial_number") == selected_trial_number:
            selected_returns = returns
    variance = float(pd.Series(sharpes, dtype="float64").var(ddof=1)) if len(sharpes) > 1 else None
    return deflated_sharpe_details(
        selected_returns,
        attempted_variants=attempted_variants,
        trial_sharpe_variance=variance,
        trial_scope=scope,
    )


def _same_immutable_trial_conditions(results: list[Mapping[str, object]]) -> bool:
    if not results:
        return False
    conditions: list[tuple[object, ...]] = []
    for result in results:
        configuration = result.get("configuration")
        data = result.get("data")
        costs = result.get("costs")
        if not isinstance(configuration, Mapping) or not isinstance(data, Mapping):
            return False
        condition = (
            data.get("fingerprint", configuration.get("dataset_fingerprint")),
            data.get("symbol", configuration.get("symbol")),
            data.get("timeframe", configuration.get("timeframe")),
            data.get("start"),
            data.get("end"),
            configuration.get("cashflow"),
            configuration.get("risk_budget"),
            configuration.get("execution_model"),
            configuration.get("benchmark"),
            configuration.get("cost_model", costs),
        )
        if any(value in (None, "", {}, []) for value in condition):
            return False
        conditions.append(condition)
    return all(condition == conditions[0] for condition in conditions[1:])


def _validation_statistics(
    oos_returns: pd.Series | None,
    candidates: list[dict[str, float | int | str | bool]],
    folds: list[JsonDict],
    *,
    cscv_diagnostic: list[list[float]] | None,
    dataset_id: str,
) -> JsonDict:
    scope: JsonDict = {
        "candidate_count": len(candidates),
        "fold_count": len(folds),
        "candidate_evaluations_before_selection": len(candidates) * len(folds),
        "selection_events": len(folds),
        "effective_independent_trials": None,
    }
    dsr = deflated_sharpe_details(
        oos_returns,
        attempted_variants=len(candidates) * len(folds),
        trial_sharpe_variance=None,
        trial_scope=scope,
    )
    if cscv_diagnostic is None:
        pbo: JsonDict = {
            "status": "not_applicable",
            "reason": (
                "strict walk-forward runs only the validation-selected candidate on each locked "
                "OOS test window; CSCV requires a separately requested all-candidate OOS matrix"
            ),
        }
    elif len(candidates) < 8 or len(folds) < 4 or any(
        len(scores) != len(folds) for scores in cscv_diagnostic
    ):
        pbo = {
            "status": "not_applicable",
            "reason": "CSCV diagnostic requires at least 8 candidates and 4 complete OOS folds",
        }
    else:
        from qt.legacy.btcqt.backtest.deflate import pbo_estimate

        first_window = _mapping(folds[0].get("fold"), "first fold")
        last_window = _mapping(folds[-1].get("fold"), "last fold")
        span = (
            f"{dataset_id}:{_text(first_window.get('test_start'), 'test_start')}:"
            f"{_text(last_window.get('test_end'), 'test_end')}"
        )
        estimate = pbo_estimate(
            [
                {"span": span, "walkforward_sharpes": scores}
                for scores in cscv_diagnostic
            ]
        )
        value = estimate.get("pbo")
        pbo = (
            {
                "status": "computed",
                "method": "legacy_cscv_style_oos_diagnostic",
                "scope": {"span": span, "candidate_count": len(candidates)},
                **estimate,
            }
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else {"status": "not_applicable", "reason": str(estimate.get("note", "CSCV unavailable"))}
        )
    return {"deflated_sharpe": dsr, "pbo": pbo}


def _objective_value(result: Mapping[str, object], objective: str) -> float:
    metrics = result.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("native executor result has no metrics")
    aliases = {
        "net_return": ("net_return", "total_return", "return_pct"),
        "sharpe": ("sharpe",),
        "calmar": ("calmar",),
    }
    for key in aliases[objective]:
        value = metrics.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            objective_value = float(value)
            if isfinite(objective_value):
                return objective_value
            raise InvalidObjectiveError(f"native executor result has non-finite {objective}")
    raise InvalidObjectiveError(f"native executor result has no finite {objective} metric")


def _job_type(job: Mapping[str, object]) -> str | None:
    spec = job.get("spec")
    if not isinstance(spec, Mapping):
        return None
    value = spec.get("job_type")
    return value if isinstance(value, str) else None


def _explain_content(content: StrategyContent, values: Mapping[str, float]) -> list[JsonDict]:
    explainers: list[JsonDict] = []
    if content.entry_rule is not None:
        explainers.append({"stage": "entry", **_explain_node(content.entry_rule, values)})
    if content.exit_rule is not None:
        explainers.append({"stage": "exit", **_explain_node(content.exit_rule, values)})
    for regime in content.regimes:
        explainers.append({"stage": f"regime:{regime.name}", **_explain_node(regime.when, values)})
    return explainers


def _explain_node(node: object, values: Mapping[str, float]) -> JsonDict:
    dumped = node.model_dump(mode="json") if hasattr(node, "model_dump") else {}
    kind = str(dumped.get("kind"))
    if isinstance(node, RuleGroup):
        children = [_explain_node(child, values) for child in node.children]
        known = [item["satisfied"] for item in children if item["satisfied"] is not None]
        satisfied = (
            (all(known) if kind == "and" else any(known)) if len(known) == len(children) else None
        )
        if kind == "not" and satisfied is not None:
            satisfied = not satisfied
        return {
            "rule": kind.upper(),
            "satisfied": satisfied,
            "explanation_zh": {
                "and": "所有条件必须同时成立。",
                "or": "任一条件成立即可。",
                "not": "子条件不成立时本条件成立。",
            }[kind],
            "children": children,
        }
    if isinstance(node, SustainedRule):
        child = _explain_node(node.child, values)
        return {
            "rule": f"sustained for {dumped['bars']} bars",
            "satisfied": None,
            "explanation_zh": f"子条件必须连续 {dumped['bars']} 根已收盘K线成立。静态校验不伪造历史连续性。",
            "child": child,
        }
    if kind == "cross":
        left = _indicator_label(dumped["left"])
        right = _indicator_label(dumped["right"])
        return {
            "rule": f"{left} crosses {dumped['direction']} {right}",
            "satisfied": None,
            "explanation_zh": f"需要确认本根已收盘K线发生{('上穿' if dumped['direction'] == 'above' else '下穿')}；不读取未来K线。",
            "values_required": [left, right],
        }
    left = _indicator_label(dumped["left"])
    right = dumped["right"]
    left_value = values.get(left)
    right_value = values.get(_indicator_label(right)) if isinstance(right, dict) else right
    satisfied = _compare(left_value, str(dumped["comparator"]), right_value)
    return {
        "rule": f"{left} {dumped['comparator']} {right_value}",
        "satisfied": satisfied,
        "explanation_zh": f"比较当时已知的 {left} 与阈值；缺少数值时不宣称条件成立。",
        "observed": {"left": left_value, "right": right_value},
    }


def _indicator_label(value: object) -> str:
    if not isinstance(value, Mapping):
        return str(value)
    parameters = value.get("parameters", {})
    return f"{value.get('indicator')}({','.join(f'{key}={parameters[key]}' for key in sorted(parameters))})"


def _compare(left: object, comparator: str, right: object) -> bool | None:
    if (
        not isinstance(left, (int, float))
        or isinstance(left, bool)
        or not isinstance(right, (int, float))
        or isinstance(right, bool)
    ):
        return None
    return {
        ">": left > right,
        ">=": left >= right,
        "<": left < right,
        "<=": left <= right,
        "==": left == right,
    }[comparator]


def _missing_comparability_fields(group: object) -> list[str]:
    if not isinstance(group, Mapping):
        return ["comparability_group"]
    required = (
        "dataset_fingerprint",
        "instrument",
        "timeframe",
        "period",
        "cashflow",
        "risk_budget",
        "execution_model",
        "benchmark",
        "cost_model",
    )
    return [name for name in required if group.get(name) in (None, "", [], {})]


def _comparability_reasons(baseline: object, candidate: object) -> list[str]:
    missing = _missing_comparability_fields(candidate)
    if missing:
        return [f"missing immutable comparison condition: {name}" for name in missing]
    if not isinstance(baseline, Mapping) or not isinstance(candidate, Mapping):
        return ["missing comparability group"]
    return [
        f"different immutable comparison condition: {name}"
        for name in candidate
        if candidate.get(name) != baseline.get(name)
    ]


def _temporal_integrity_reasons(summary: object) -> list[str]:
    if not isinstance(summary, Mapping):
        return []
    integrity = summary.get("temporal_integrity")
    if not isinstance(integrity, Mapping) or integrity.get("strategy_mode") != "plugin":
        return []
    if integrity.get("status") == "verified" and integrity.get("verified_leaderboard_eligible") is not False:
        audit = summary.get("causal_input_audit")
        if _is_independent_causal_input_audit(audit):
            return []
        return ["plugin result has no independent causal-input audit"]
    if integrity.get("verified_leaderboard_eligible") is False:
        return ["plugin result is not eligible for verified comparison or leaderboard"]
    reason = integrity.get("reason")
    detail = str(reason) if isinstance(reason, str) else "no independent causal-input audit"
    return [f"temporal integrity is unverified for plugin result: {detail}"]


def _with_temporal_integrity(
    result: Mapping[str, object],
    spec_or_version: Mapping[str, object] | None = None,
) -> JsonDict:
    """Attach plugin integrity evidence without inferring it from sandboxing."""

    materialized = dict(result)
    version: Mapping[str, object] | None = None
    if spec_or_version is not None:
        candidate = spec_or_version.get("lab_strategy_version", spec_or_version)
        if isinstance(candidate, Mapping):
            version = candidate
    if version is None:
        candidate = result.get("lab_strategy_version")
        if isinstance(candidate, Mapping):
            version = candidate
    if version is None:
        return materialized
    content = version.get("content")
    if not isinstance(content, Mapping) or content.get("mode") != "plugin":
        return materialized
    version_id = version.get("version_id")
    runtime_integrity = result.get("temporal_integrity")
    if isinstance(runtime_integrity, Mapping):
        materialized["temporal_integrity"] = {
            **dict(runtime_integrity),
            "strategy_mode": "plugin",
            "strategy_version_id": version_id,
        }
        return materialized
    materialized["temporal_integrity"] = {
        "status": "unverified",
        "strategy_mode": "plugin",
        "basis": "isolated_runtime_is_not_a_temporal_audit",
        "strategy_version_id": version_id,
        "verified_leaderboard_eligible": False,
        "reason": "missing independent causal-input audit",
    }
    return materialized


def _is_independent_causal_input_audit(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("status") == "verified"
        and value.get("independent") is True
        and isinstance(value.get("auditor"), str)
        and bool(value["auditor"])
        and isinstance(value.get("evidence_id"), str)
        and bool(value["evidence_id"])
    )


def _bounded_series(value: object, max_points: int) -> JsonDict | None:
    if not isinstance(value, Mapping):
        return None
    bounded: JsonDict = {}
    for name in ("equity", "drawdown", "returns"):
        raw = value.get(name)
        if isinstance(raw, Mapping):
            items = list(raw.items())
        elif isinstance(raw, list):
            items = list(enumerate(raw))
        else:
            continue
        stride = max(1, (len(items) + max_points - 1) // max_points)
        bounded[name] = [{"x": str(x), "y": y} for x, y in items[::stride]]
    return bounded or None


def _correlation(series: Mapping[str, JsonDict]) -> JsonDict | None:
    returns: dict[str, pd.Series] = {}
    for result_id, data in series.items():
        raw = data.get("returns")
        if not isinstance(raw, list):
            continue
        points = [point for point in raw if isinstance(point, Mapping)]
        returns[result_id] = pd.Series({str(point.get("x")): point.get("y") for point in points})
    if len(returns) < 2:
        return None
    frame = pd.DataFrame(returns).apply(pd.to_numeric, errors="coerce")
    if len(frame.dropna(how="all")) < 2:
        return None
    return {
        column: {row: value for row, value in values.items()}
        for column, values in frame.corr(min_periods=2).to_dict().items()
    }
