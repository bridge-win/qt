from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qt.lab.persistence import LabConflictError, LabRepository, NonFiniteNativeResultError
from qt.lab.router import LabSettings, build_lab_router
from qt.lab.schemas import (
    CompareRequest,
    CreateStrategyRequest,
    CreateVersionRequest,
    OptimizationRequest,
    ValidateStrategyRequest,
    ValidationRequest,
)
from qt.lab.service import (
    InvalidObjectiveError,
    LabService,
    ParquetTimelineProvider,
    _native_spec,
    _objective_value,
    _optimization_dsr,
    _resume_sampler_seed,
    _validation_statistics,
)
from qt.research.repository import IdempotencyConflictError, ResearchRepository
from qt.research.strategies import _bind_lab_parameters, _lab_rule_matches


class RecordingExecutor:
    def __init__(self) -> None:
        self.specs: list[dict[str, object]] = []

    def execute(
        self,
        spec: Mapping[str, object],
        progress: Callable[[str, int], None],
        cancelled: Callable[[], bool],
    ) -> dict[str, object]:
        assert not cancelled()
        self.specs.append(dict(spec))
        progress("native_execution", 60)
        return {
            "run_id": "native-run-1",
            "configuration": {
                "dataset_fingerprint": "f",
                "execution_model": "nautilus",
                "cashflow": "same",
                "risk_budget": "same",
                "benchmark": "buy_and_hold",
                "cost_model": "fixed",
            },
            "data": {
                "fingerprint": "f",
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "start": "2024-01-01",
                "end": "2024-02-01",
            },
            "metrics": {"sharpe": 1.2, "calmar": 0.8, "net_return": 0.1},
            "decision_traces": [
                {"known_at": "2024-01-01T01:00:00Z", "decision": "enter"},
                {"known_at": "2024-01-01T02:00:00Z", "decision": "hold"},
            ],
        }


class SelectionExecutor(RecordingExecutor):
    def execute(
        self,
        spec: Mapping[str, object],
        progress: Callable[[str, int], None],
        cancelled: Callable[[], bool],
    ) -> dict[str, object]:
        result = super().execute(spec, progress, cancelled)
        version = spec["lab_strategy_version"]
        assert isinstance(version, Mapping)
        parameters = version["parameter_overrides"]
        assert isinstance(parameters, Mapping)
        result["metrics"] = {"sharpe": float(parameters.get("period", 0))}
        result["series"] = {"returns": {"0": 0.01, "1": -0.005, "2": 0.02}}
        return result


class FixedTimeline:
    def timestamps(self, dataset_id: str) -> pd.DatetimeIndex:
        assert dataset_id == "btc"
        return pd.date_range("2024-01-01", periods=80, freq="h", tz="UTC")


def _rules() -> dict[str, object]:
    return {
        "kind": "rules",
        "parameters": {"period": {"value": 14, "minimum": 2, "maximum": 100}},
        "rules": {
            "entry": {
                "kind": "signal",
                "signal": {
                    "indicator": "rsi",
                    "timeframe": "current",
                    "parameters": {"period": "${period}"},
                },
                "comparator": "<",
                "right": 30,
            },
            "exit": {
                "kind": "sustained_for",
                "bars": 2,
                "child": {
                    "kind": "crosses_below",
                    "left": {"indicator": "sma", "timeframe": "1h", "parameters": {"window": 20}},
                    "right": {"indicator": "sma", "timeframe": "1h", "parameters": {"window": 60}},
                },
            },
        },
    }


def _native_rules() -> dict[str, object]:
    return {
        "kind": "rules",
        "rules": {
            "entry": {
                "kind": "signal",
                "signal": {
                    "indicator": "sma",
                    "timeframe": "current",
                    "parameters": {"window": 5},
                },
                "comparator": ">",
                "right": -1,
            },
            "exit": {
                "kind": "signal",
                "signal": {
                    "indicator": "sma",
                    "timeframe": "current",
                    "parameters": {"window": 5},
                },
                "comparator": "<",
                "right": -1,
            },
        },
    }


def _service(
    path: Path,
    executor: RecordingExecutor | None = None,
    *,
    timeline: FixedTimeline | None = None,
) -> LabService:
    return LabService(
        LabRepository(path),
        executor,
        timeline_provider=timeline,
        research_repository=ResearchRepository(path, queue_limit=20),
    )


def _execute_shared_job(
    service: LabService,
    job: Mapping[str, object],
    claimed_job: Mapping[str, object] | None = None,
) -> dict[str, object]:
    queue = service.research_repository
    claimed = claimed_job or queue.claim_next("lab-test-worker")
    assert claimed is not None
    assert claimed["job_id"] == job["job_id"]
    attempt_id = str(claimed["attempt_id"])
    job_id = str(claimed["job_id"])

    def progress(stage: str, percent: int) -> None:
        queue.update_progress(job_id, stage, percent, attempt_id=attempt_id)
        assert queue.heartbeat_attempt(job_id, attempt_id=attempt_id)

    def cancelled() -> bool:
        return queue.is_cancel_requested(job_id, attempt_id=attempt_id)

    try:
        result = service.execute(
            {**dict(claimed["spec"]), "lab_job_id": job_id}, progress, cancelled
        )
    except Exception as error:
        if cancelled():
            return queue.cancel_running(job_id, attempt_id=attempt_id)
        return queue.fail(job_id, str(error), attempt_id=attempt_id)
    return queue.complete(job_id, result, attempt_id=attempt_id)


def test_web_shape_persists_immutable_versions_and_explains_real_rules(tmp_path: Path) -> None:
    service = _service(tmp_path / "research.sqlite3")
    repository = service.repository
    created = service.create_strategy(
        CreateStrategyRequest.model_validate({"name": "rule draft", **_rules()})
    )
    strategy_id = str(created["strategy"]["strategy_id"])
    first = created["version"]
    assert first is not None
    validation = service.validate(ValidateStrategyRequest.model_validate(_rules()))
    assert (
        validation["explainers"][1]["child"]["rule"]
        == "sma(window=20) crosses below sma(window=60)"
    )
    second = service.create_version(
        strategy_id,
        CreateVersionRequest.model_validate(
            {"expected_version": 1, "message": "immutable", **_rules()}
        ),
    )
    assert second["revision"] == 2
    with pytest.raises(LabConflictError):
        service.create_version(
            strategy_id, CreateVersionRequest.model_validate({"expected_version": 1, **_rules()})
        )
    assert [item["revision"] for item in repository.list_versions(strategy_id)] == [2, 1]


def test_unified_catalog_exposes_registry_and_preserved_profiles_without_proxy_rewrite(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "research.sqlite3")
    builtins = service.catalog.list_builtins()
    assert len(builtins) >= 123
    clone = service.create_strategy(
        CreateStrategyRequest(name="preserve source", base_strategy_id="qt:sma_crossover")
    )
    version = clone["version"]
    assert version is not None
    assert version["content"]["mode"] == "builtin"
    assert version["content"]["builtin_identity"] == "qt:sma_crossover"
    rsi_doc = service.indicator_documentation("rsi")
    assert rsi_doc["source"] == "qt.indicators.talib_standard.rsi"
    assert rsi_doc["parameters"][1]["name"] == "period"
    assert rsi_doc["availability_basis"] == "completed_market_bar"
    assert rsi_doc["may_be_revised"] is False
    derivatives_doc = service.indicator_documentation("derivatives.funding_zscore")
    assert derivatives_doc["availability_basis"] == "provider_recorded_available_at"
    assert derivatives_doc["requires_provider_available_at"] is True
    assert derivatives_doc["may_be_revised"] is True
    assert service.indicator_documentation("events.flash_crash")["availability_basis"] == "completed_market_bar"
    assert service.indicator_documentation("price.bollinger_bands")["formula"] is not None
    assert service.indicator_documentation("sma")["source"] == "qt.workbench.strategy_factory.build_lab_strategy"


def test_router_accepts_contract_shape_and_generic_immutable_note_binding(tmp_path: Path) -> None:
    service = _service(tmp_path / "research.sqlite3")
    app = FastAPI()
    app.include_router(build_lab_router(LabSettings(tmp_path), service=service), prefix="/api/v3")
    client = TestClient(app)
    created = client.post("/api/v3/strategies", json={"name": "from web", **_rules()})
    assert created.status_code == 201
    version_id = created.json()["version"]["version_id"]
    note = client.post(
        "/api/v3/notes",
        json={"entity_type": "strategy_version", "entity_id": version_id, "body": "reproducible"},
    )
    assert note.status_code == 201
    assert note.json()["strategy_version_id"] == version_id
    invalid = client.post(
        "/api/v3/notes",
        json={"entity_type": "experiment", "entity_id": "exp", "body": "missing binding"},
    )
    assert invalid.status_code == 422
    validation_payload = {"strategy_version_id": version_id, "experiment": {"dataset_id": "btc"}}
    headers = {"Idempotency-Key": "web-retry"}
    first_job = client.post("/api/v3/validation", json=validation_payload, headers=headers)
    retry = client.post("/api/v3/validation", json=validation_payload, headers=headers)
    assert first_job.status_code == retry.status_code == 202
    assert first_job.json()["created"] is True
    assert retry.json()["created"] is False
    assert retry.json()["job"]["job_id"] == first_job.json()["job"]["job_id"]
    changed = client.post(
        "/api/v3/validation",
        json={"strategy_version_id": version_id, "experiment": {"dataset_id": "btc-elsewhere"}},
        headers=headers,
    )
    assert changed.status_code == 409


def test_trace_alias_filters_timezone_aware_timestamped_traces(tmp_path: Path) -> None:
    service = _service(tmp_path / "research.sqlite3")
    service.import_native_result(
        {"run_id": "trace-run", "metrics": {"sharpe": 1.0}},
        [
            {"timestamp": "2024-01-01T00:00:00Z", "decision": "wait"},
            {"timestamp": "2024-01-02T00:00:00Z", "decision": "enter"},
        ],
    )
    app = FastAPI()
    app.include_router(build_lab_router(LabSettings(tmp_path), service=service), prefix="/api/v3")
    client = TestClient(app)
    response = client.get(
        "/api/v3/traces",
        params={
            "experiment_id": "trace-run",
            "from": "2024-01-01T12:00:00Z",
            "to": "2024-01-02T12:00:00Z",
            "limit": 10,
        },
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["trace"]["decision"] == "enter"
    invalid = client.get("/api/v3/traces", params={"experiment_id": "trace-run", "from": "2024-01-01T00:00:00Z"})
    assert invalid.status_code == 422


def test_validation_job_invokes_injected_native_boundary_and_pages_decision_traces(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor()
    service = _service(tmp_path / "research.sqlite3", executor)
    repository = service.repository
    created = service.create_strategy(
        CreateStrategyRequest.model_validate({"name": "native", **_rules()})
    )
    version_id = str(created["version"]["version_id"])
    queued, created_job = service.enqueue_validation(
        ValidationRequest(
            strategy_version_id=version_id, experiment={"dataset_id": "btc"}, profile="quick"
        ),
        idempotency_key="validation-key",
    )
    assert created_job and queued["status"] == "queued"
    completed = _execute_shared_job(service, queued)
    assert completed["status"] == "complete"
    assert executor.specs[0]["lab_execution_kind"] == "quick_diagnostic"
    assert repository.traces("native-run-1", None, 1)["next_cursor"] == 1
    comparison = service.compare(CompareRequest(result_ids=["native-run-1", "native-run-1"]))
    assert comparison["comparable_result_ids"] == ["native-run-1", "native-run-1"]


def test_optimization_budget_is_bounded_before_any_worker_execution() -> None:
    with pytest.raises(ValueError, match="grid budget"):
        OptimizationRequest(
            strategy_version_id="version",
            experiment={"dataset_id": "btc"},
            search_space={"period": [10, 20]},
            sampler="grid",
            objective="sharpe",
            budget=3,
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_optimization_objectives_are_rejected(value: float) -> None:
    with pytest.raises(InvalidObjectiveError, match="non-finite"):
        _objective_value({"metrics": {"sharpe": value}}, "sharpe")


def test_resumed_random_and_tpe_sampler_streams_are_deterministic_but_not_restarted() -> None:
    first = _resume_sampler_seed(7, "immutable-job", 1)
    assert first == _resume_sampler_seed(7, "immutable-job", 1)
    assert first != _resume_sampler_seed(7, "immutable-job", 2)
    assert first != _resume_sampler_seed(8, "immutable-job", 1)


def test_optimization_dsr_uses_comparable_nonannualized_trial_sharpes() -> None:
    common = {
        "configuration": {
            "dataset_fingerprint": "fingerprint",
            "cashflow": "weekly",
            "risk_budget": "one_x",
            "execution_model": "nautilus",
            "benchmark": "buy_and_hold",
            "cost_model": "fees_v1",
        },
        "data": {
            "fingerprint": "fingerprint",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "start": "2024-01-01",
            "end": "2024-02-01",
        },
        "costs": {"fees": 1.0},
    }
    statistics = _optimization_dsr(
        [
            {
                "trial_number": 0,
                "state": "COMPLETE",
                "result": {**common, "series": {"returns": {"a": 0.01, "b": -0.005, "c": 0.008}}},
            },
            {
                "trial_number": 1,
                "state": "COMPLETE",
                "result": {**common, "series": {"returns": {"a": 0.02, "b": -0.006, "c": 0.01}}},
            },
        ],
        attempted_variants=2,
        selected_trial_number=1,
    )
    assert statistics["status"] == "computed"
    assert statistics["multiple_testing_scope"]["sharpe_scale"] == "per_bar_nonannualized"
    assert statistics["multiple_testing_scope"]["trial_sharpe_variance_estimator"] == "sample_variance_ddof_1"
    assert statistics["trial_sharpe_variance"] is not None


def test_optuna_persists_invalid_and_valid_trials_without_aborting_search(tmp_path: Path) -> None:
    pytest.importorskip("optuna")

    class NonFiniteThenFiniteExecutor(RecordingExecutor):
        def execute(
            self,
            spec: Mapping[str, object],
            progress: Callable[[str, int], None],
            cancelled: Callable[[], bool],
        ) -> dict[str, object]:
            result = super().execute(spec, progress, cancelled)
            result["metrics"] = {"sharpe": float("nan") if len(self.specs) == 1 else 1.0}
            return result

    executor = NonFiniteThenFiniteExecutor()
    service = _service(tmp_path / "research.sqlite3", executor)
    created = service.create_strategy(
        CreateStrategyRequest.model_validate({"name": "optuna", **_rules()})
    )
    version = created["version"]
    assert isinstance(version, Mapping)
    payload = OptimizationRequest(
        strategy_version_id=str(version["version_id"]),
        experiment={"dataset_id": "btc"},
        search_space={"period": [5, 20]},
        sampler="grid",
        objective="sharpe",
        budget=2,
    ).model_dump(mode="json")
    payload["strategy_version"] = version
    result = service._run_optimization(
        "optuna-job", payload, lambda _stage, _percent: None, lambda: False
    )
    assert result["trials"] == 2
    assert [trial["state"] for trial in service.repository.list_trials("optuna-job")] == [
        "FAIL",
        "COMPLETE",
    ]


@pytest.mark.parametrize("sampler", ["random", "tpe"])
def test_optuna_retry_uses_a_deterministic_new_sampler_stream(
    tmp_path: Path, sampler: str
) -> None:
    pytest.importorskip("optuna")

    class InterruptOnSecondTrial(RecordingExecutor):
        def execute(
            self,
            spec: Mapping[str, object],
            progress: Callable[[str, int], None],
            cancelled: Callable[[], bool],
        ) -> dict[str, object]:
            if self.specs:
                raise RuntimeError("worker interrupted after durable first trial")
            return super().execute(spec, progress, cancelled)

    first_service = _service(tmp_path / "research.sqlite3", InterruptOnSecondTrial())
    created = first_service.create_strategy(
        CreateStrategyRequest.model_validate({"name": "retry", **_rules()})
    )
    version = created["version"]
    assert isinstance(version, Mapping)
    payload = OptimizationRequest(
        strategy_version_id=str(version["version_id"]),
        experiment={"dataset_id": "btc"},
        search_space={"period": [5, 20, 30]},
        sampler=sampler,
        objective="sharpe",
        budget=3,
        seed=17,
    ).model_dump(mode="json")
    payload["strategy_version"] = version
    with pytest.raises(RuntimeError, match="interrupted"):
        first_service._run_optimization(
            "retry-job", payload, lambda _stage, _percent: None, lambda: False
        )
    resumed_service = _service(tmp_path / "research.sqlite3", RecordingExecutor())
    result = resumed_service._run_optimization(
        "retry-job", payload, lambda _stage, _percent: None, lambda: False
    )
    sampler_state = result["sampler_state"]
    assert isinstance(sampler_state, Mapping)
    assert sampler_state["persisted_trials_before"] == 2
    assert sampler_state["effective_seed"] == _resume_sampler_seed(17, "retry-job", 2)


def test_strict_validation_and_compare_use_real_native_executor_payload(tmp_path: Path) -> None:
    pytest.importorskip("nautilus_trader")
    from qt.nautilus.executor import NautilusResearchExecutor
    from qt.workbench.reports import R2ReportPublisher

    parquet_root = tmp_path / "parquet"
    ohlcv = parquet_root / "ohlcv"
    ohlcv.mkdir(parents=True)
    index = pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC")
    close = pd.Series(range(100, 200), index=index, dtype="float64")
    pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 10.0,
        },
        index=index,
    ).to_parquet(ohlcv / "okx_BTCUSDT_1h.parquet")
    database = tmp_path / "research.sqlite3"
    artifact_root = tmp_path / "artifacts"
    service = LabService(
        LabRepository(database),
        NautilusResearchExecutor(parquet_root, artifact_root),
        timeline_provider=ParquetTimelineProvider(parquet_root),
        research_repository=ResearchRepository(database, queue_limit=20),
        result_publisher=R2ReportPublisher(None, artifact_root=artifact_root),
    )
    created = service.create_strategy(
        CreateStrategyRequest.model_validate({"name": "native strict", **_native_rules()})
    )
    version = created["version"]
    assert isinstance(version, Mapping)
    queued, _ = service.enqueue_validation(
        ValidationRequest(
            strategy_version_id=str(version["version_id"]),
            experiment={"dataset_id": "okx-btcusdt-1h"},
            train_bars=20,
            validation_bars=10,
            test_bars=10,
            folds=2,
            purge_bars=1,
            embargo_bars=1,
        ),
        idempotency_key="native-strict",
    )
    completed = _execute_shared_job(service, queued)
    assert completed["status"] == "complete", {
        "job_id": queued["job_id"],
        "stage": completed.get("stage"),
        "progress": completed.get("progress"),
        "error": completed.get("error"),
        "result": completed.get("result"),
    }
    result = completed["result"]
    assert isinstance(result, Mapping)
    folds = result["folds"]
    assert isinstance(folds, list)
    result_ids = [str(fold["test_result_id"]) for fold in folds if isinstance(fold, Mapping)]
    for result_id in result_ids:
        summary = service.repository.get_result(result_id)["summary"]
        assert isinstance(summary, Mapping)
        account_invariants = summary.get("account_invariants")
        assert isinstance(account_invariants, Mapping), summary
        assert account_invariants.get("status") == "verified", account_invariants
        assert account_invariants.get("violations") == [], account_invariants
        assert summary["report_publication"] == {"status": "not_configured", "bucket": None}
        artifacts = summary.get("artifacts")
        assert isinstance(artifacts, list) and artifacts
        assert all("path" not in artifact for artifact in artifacts if isinstance(artifact, Mapping))
        assert all(
            artifact.get("publication_status") == "r2_not_configured"
            for artifact in artifacts
            if isinstance(artifact, Mapping)
        )
        traces = service.repository.traces(result_id, None, 20)
        assert traces["items"]
        assert all(
            isinstance(item["trace"].get("timestamp"), str)
            and isinstance(item["trace"].get("observed_values"), Mapping)
            for item in traces["items"]
            if isinstance(item, Mapping) and isinstance(item.get("trace"), Mapping)
        )
    comparison = service.compare(CompareRequest(result_ids=result_ids))
    assert comparison["comparable_result_ids"] == result_ids
    assert comparison["monthly_returns"] is not None
    assert set(comparison["monthly_returns"]) == set(result_ids)


def test_parameter_bindings_change_the_actual_lab_rule_evaluation() -> None:
    content = CreateStrategyRequest.model_validate(
        {
            "name": "bound",
            "kind": "rules",
            "parameters": {"threshold": {"value": 5, "minimum": 0, "maximum": 20}},
            "rules": {
                "entry": {
                    "kind": "signal",
                    "signal": {
                        "indicator": "sma",
                        "timeframe": "current",
                        "parameters": {"window": 10},
                    },
                    "comparator": ">",
                    "right": "${threshold}",
                },
                "exit": {
                    "kind": "signal",
                    "signal": {
                        "indicator": "sma",
                        "timeframe": "current",
                        "parameters": {"window": 10},
                    },
                    "comparator": "<",
                    "right": -1,
                },
            },
        }
    ).content
    assert content is not None
    serialized = content.model_dump(mode="json")
    bars = pd.DataFrame({"close": list(range(1, 21))})
    low_threshold = _bind_lab_parameters(serialized, {"threshold": 5})
    high_threshold = _bind_lab_parameters(serialized, {"threshold": 16})
    assert _lab_rule_matches(bars, low_threshold["entry_rule"]) is True
    assert _lab_rule_matches(bars, high_threshold["entry_rule"]) is False
    spec = _native_spec(
        {
            "seed": 7,
            "experiment": {"dataset_id": "btc"},
            "strategy_version": {"version_id": "immutable", "content": serialized},
        },
        execution_kind="optimization_trial",
        parameter_overrides={"threshold": 16},
    )
    assert spec["parameter_overrides"] == {"threshold": 16}
    version = spec["lab_strategy_version"]
    assert isinstance(version, Mapping)
    assert version["content"] == serialized


def test_composite_execution_hydrates_exact_bounded_immutable_versions(tmp_path: Path) -> None:
    service = _service(tmp_path / "research.sqlite3")
    left = service.create_strategy(
        CreateStrategyRequest.model_validate({"name": "left", **_rules()})
    )
    right = service.create_strategy(
        CreateStrategyRequest.model_validate({"name": "right", **_rules()})
    )
    left_version = left["version"]
    right_version = right["version"]
    assert isinstance(left_version, Mapping)
    assert isinstance(right_version, Mapping)
    composite = service.create_strategy(
        CreateStrategyRequest.model_validate(
            {
                "name": "ensemble",
                "kind": "ensemble",
                "ensemble": [
                    {"strategy_version_id": left_version["version_id"], "target_weight": 0.4},
                    {"strategy_version_id": right_version["version_id"], "target_weight": 0.6},
                ],
            }
        )
    )
    version = composite["version"]
    assert isinstance(version, Mapping)
    execution_version = service.execution_version(str(version["version_id"]))
    execution_content = execution_version["content"]
    assert isinstance(execution_content, Mapping)
    members = execution_content["ensemble"]
    assert isinstance(members, list)
    assert all(isinstance(member.get("strategy_version"), Mapping) for member in members)
    spec = service._native_spec(
        {
            "seed": 7,
            "experiment": {"dataset_id": "btc"},
            "strategy_version": version,
        },
        execution_kind="quick_diagnostic",
        parameter_overrides={},
    )
    execution_version = spec["lab_strategy_version"]
    assert isinstance(execution_version, Mapping)
    execution_content = execution_version["content"]
    assert isinstance(execution_content, Mapping)
    members = execution_content["ensemble"]
    assert isinstance(members, list)
    nested = [member["strategy_version"] for member in members if isinstance(member, Mapping)]
    assert [item["version_id"] for item in nested if isinstance(item, Mapping)] == [
        left_version["version_id"],
        right_version["version_id"],
    ]
    assert [item["content"] for item in nested if isinstance(item, Mapping)] == [
        left_version["content"],
        right_version["content"],
    ]


def test_idempotent_submissions_are_transactional_and_reject_changed_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.sqlite3"
    repository = ResearchRepository(path, queue_limit=20)

    def submit() -> tuple[dict[str, object], bool]:
        return repository.enqueue_idempotent(
            {"job_type": "lab_validation", "payload": {"version": "immutable", "seed": 7}},
            idempotency_key="same-key",
        )

    with ThreadPoolExecutor(max_workers=4) as workers:
        submissions = list(workers.map(lambda _: submit(), range(4)))
    job_ids = {str(job["job_id"]) for job, _ in submissions}
    assert len(job_ids) == 1
    assert sum(1 for _, created in submissions if created) == 1
    with pytest.raises(IdempotencyConflictError, match="different request"):
        repository.enqueue_idempotent(
            {"job_type": "lab_validation", "payload": {"version": "immutable", "seed": 8}},
            idempotency_key="same-key",
        )
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'lab_%'"
            )
        }
    assert "lab_jobs" not in tables
    assert "lab_submission_keys" not in tables


def test_result_detail_imports_existing_shared_native_run(tmp_path: Path) -> None:
    path = tmp_path / "research.sqlite3"
    research = ResearchRepository(path)
    job = research.enqueue({"dataset_id": "btc"})
    research.claim_next("worker")
    research.complete(
        str(job["job_id"]),
        {
            "run_id": "existing-native-run",
            "configuration": {"execution_model": "nautilus"},
            "data": {"fingerprint": "native", "symbol": "BTC/USDT", "timeframe": "1h"},
            "metrics": {"sharpe": 1.0},
            "decision_traces": [{"decision": "hold"}],
        },
    )
    repository = LabRepository(path)
    result = repository.get_result("existing-native-run")
    assert result["source"] == "research_runs"
    assert repository.traces("existing-native-run", None, 20)["source"] == "research_runs"
    assert _service(path).list_results(limit=1)[0]["result_id"] == "existing-native-run"


def test_compare_returns_bounded_server_series_only_for_complete_matching_conditions(
    tmp_path: Path,
) -> None:
    repository = LabRepository(tmp_path / "research.sqlite3")
    common = {
        "configuration": {
            "dataset_fingerprint": "fingerprint",
            "execution_model": "nautilus",
            "cashflow": "weekly",
            "risk_budget": "one_x",
            "benchmark": "buy_and_hold",
            "cost_model": "fees_v1",
        },
        "data": {
            "fingerprint": "fingerprint",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "start": "2024-01-01",
            "end": "2024-02-01",
        },
        "costs": {"fees": 1.0},
        "monthly_returns": {"2024-01": 0.1},
    }
    repository.store_result(
        {
            **common,
            "run_id": "left",
            "metrics": {"sharpe": 1.0},
            "series": {"returns": {"a": 0.01, "b": 0.02}, "equity": {"a": 100, "b": 102}},
        },
        [],
    )
    repository.store_result(
        {
            **common,
            "run_id": "right",
            "metrics": {"sharpe": 1.1},
            "series": {"returns": {"a": 0.02, "b": 0.03}, "drawdown": {"a": 0, "b": -0.01}},
        },
        [],
    )
    comparison = _service(tmp_path / "research.sqlite3").compare(
        CompareRequest(result_ids=["left", "right"], max_points=10)
    )
    assert comparison["comparable_result_ids"] == ["left", "right"]
    assert comparison["series"] is not None
    assert comparison["correlation"] is not None
    repository.store_result({"run_id": "missing", "metrics": {"sharpe": 0.0}}, [])
    incomplete = _service(tmp_path / "research.sqlite3").compare(
        CompareRequest(result_ids=["left", "missing"])
    )
    assert incomplete["comparable_result_ids"] == ["left"]
    assert incomplete["incomparable"][0]["result_id"] == "missing"


def test_native_results_reject_nonfinite_values_but_preserve_null_metric_reasons(tmp_path: Path) -> None:
    repository = LabRepository(tmp_path / "research.sqlite3")
    with pytest.raises(NonFiniteNativeResultError, match=r"result\.metrics\.sharpe"):
        repository.store_result(
            {"run_id": "nonfinite", "metrics": {"sharpe": float("nan")}}, []
        )
    stored = repository.store_result(
        {
            "run_id": "undefined-metric",
            "metrics": {"sharpe": None},
            "metric_reasons": {"sharpe": "fewer than three marked-to-market returns"},
        },
        [],
    )
    assert stored["summary"]["metrics"]["sharpe"] is None
    assert stored["summary"]["metric_reasons"]["sharpe"] == "fewer than three marked-to-market returns"


def test_plugin_runtime_integrity_is_preserved_and_excluded_until_independently_audited(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "research.sqlite3")
    created = service.create_strategy(
        CreateStrategyRequest.model_validate(
            {
                "name": "unverified plugin",
                "kind": "plugin",
                "code": "def build_strategy(): return None",
                "plugin_api_version": "v1",
            }
        )
    )
    plugin_version = created["version"]
    assert isinstance(plugin_version, Mapping)
    common = {
        "configuration": {
            "dataset_fingerprint": "fingerprint",
            "execution_model": "nautilus",
            "cashflow": "weekly",
            "risk_budget": "one_x",
            "benchmark": "buy_and_hold",
            "cost_model": "fees_v1",
        },
        "data": {
            "fingerprint": "fingerprint",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "start": "2024-01-01",
            "end": "2024-02-01",
        },
        "metrics": {"sharpe": 1.0},
        "series": {"returns": {"a": 0.01, "b": -0.005, "c": 0.02}},
    }
    service.import_native_result({**common, "run_id": "rules"})
    service.import_native_result(
        {
            **common,
            "run_id": "plugin-unverified",
            "lab_strategy_version": plugin_version,
            "temporal_integrity": {
                "status": "unverified",
                "verified_leaderboard_eligible": False,
                "runtime": "isolated_plugin",
            },
        }
    )
    detail = service.repository.get_result("plugin-unverified")["summary"]
    assert isinstance(detail, Mapping)
    integrity = detail["temporal_integrity"]
    assert isinstance(integrity, Mapping)
    assert integrity["status"] == "unverified"
    assert integrity["verified_leaderboard_eligible"] is False
    comparison = service.compare(
        CompareRequest(result_ids=["plugin-unverified", "rules"], max_points=10)
    )
    assert comparison["comparable_result_ids"] == ["rules"]
    assert "not eligible" in comparison["incomparable"][0]["reasons"][0]
    assert comparison["metrics"][0]["temporal_integrity"] == integrity

    service.import_native_result(
        {
            **common,
            "run_id": "plugin-audited",
            "lab_strategy_version": plugin_version,
            "temporal_integrity": {
                "status": "verified",
                "verified_leaderboard_eligible": True,
            },
            "causal_input_audit": {
                "status": "verified",
                "independent": True,
                "auditor": "causal-audit-service",
                "evidence_id": "audit-001",
            },
        }
    )
    audited = service.compare(CompareRequest(result_ids=["rules", "plugin-audited"]))
    assert audited["comparable_result_ids"] == ["rules", "plugin-audited"]


def test_unverified_plugin_trial_is_excluded_from_optimizer_dsr() -> None:
    result = {
        "configuration": {
            "dataset_fingerprint": "fingerprint",
            "cashflow": "weekly",
            "risk_budget": "one_x",
            "execution_model": "nautilus",
            "benchmark": "buy_and_hold",
            "cost_model": "fees_v1",
        },
        "data": {
            "fingerprint": "fingerprint",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "start": "2024-01-01",
            "end": "2024-02-01",
        },
        "costs": {"fees": 1.0},
        "series": {"returns": {"a": 0.01, "b": -0.005, "c": 0.02}},
        "temporal_integrity": {
            "status": "unverified",
            "strategy_mode": "plugin",
            "verified_leaderboard_eligible": False,
        },
    }
    statistics = _optimization_dsr(
        [{"trial_number": 0, "state": "COMPLETE", "result": result}],
        attempted_variants=1,
        selected_trial_number=0,
    )
    assert statistics["status"] == "not_applicable"
    assert "not verified temporal evidence" in str(statistics["reason"])


def test_queued_lab_cancellation_is_terminal_and_never_claimed(tmp_path: Path) -> None:
    service = _service(tmp_path / "research.sqlite3", RecordingExecutor())
    created = service.create_strategy(
        CreateStrategyRequest.model_validate({"name": "cancel", **_rules()})
    )
    version_id = str(created["version"]["version_id"])
    queued, _ = service.enqueue_validation(
        ValidationRequest(strategy_version_id=version_id, experiment={"dataset_id": "btc"}),
        idempotency_key="cancel-key",
    )
    cancelled = service.request_cancel(str(queued["job_id"]))
    assert cancelled["status"] == "cancelled"
    assert service.research_repository.claim_next("worker") is None


def test_running_lab_cancellation_fences_late_result_publication(tmp_path: Path) -> None:
    service = _service(tmp_path / "research.sqlite3", RecordingExecutor())
    created = service.create_strategy(
        CreateStrategyRequest.model_validate({"name": "cancel", **_rules()})
    )
    version_id = str(created["version"]["version_id"])
    queued, _ = service.enqueue_validation(
        ValidationRequest(strategy_version_id=version_id, experiment={"dataset_id": "btc"}),
        idempotency_key="cancel-running-key",
    )
    claimed = service.research_repository.claim_next("worker")
    assert claimed is not None
    attempt_id = str(claimed["attempt_id"])
    assert service.request_cancel(str(queued["job_id"]))["cancel_requested"] is True
    completed = _execute_shared_job(service, queued, claimed)
    assert completed["status"] == "cancelled"
    with pytest.raises(ValueError, match="cannot complete"):
        service.research_repository.complete(
            str(queued["job_id"]), {"run_id": "late-native-result"}, attempt_id=attempt_id
        )


def test_strict_validation_selects_without_reading_fold_test_data(tmp_path: Path) -> None:
    executor = SelectionExecutor()
    service = _service(tmp_path / "research.sqlite3", executor, timeline=FixedTimeline())
    created = service.create_strategy(
        CreateStrategyRequest.model_validate({"name": "walk-forward", **_rules()})
    )
    version_id = str(created["version"]["version_id"])
    _queued, _ = service.enqueue_validation(
        ValidationRequest(
            strategy_version_id=version_id,
            experiment={"dataset_id": "btc"},
            train_bars=20,
            validation_bars=10,
            test_bars=10,
            folds=2,
            purge_bars=2,
            embargo_bars=1,
            candidate_parameters=[{"period": 5}, {"period": 20}],
        ),
        idempotency_key="strict-key",
    )
    completed = _execute_shared_job(service, _queued)
    assert completed["status"] == "complete"
    output = completed["result"]
    assert isinstance(output, dict)
    assert output["validation_status"] == "strict_walk_forward"
    folds = output["folds"]
    assert isinstance(folds, list)
    assert all(fold["selected_parameters"] == {"period": 20} for fold in folds)
    statistics = output["statistics"]
    assert isinstance(statistics, dict)
    assert statistics["deflated_sharpe"]["status"] == "not_applicable"
    assert statistics["deflated_sharpe"]["multiple_testing_scope"]["candidate_evaluations_before_selection"] == 4
    assert statistics["pbo"]["status"] == "not_applicable"
    phases = [spec["lab_phase"] for spec in executor.specs]
    assert phases.count("train") == 4
    assert phases.count("validation") == 4
    assert phases.count("test") == 2
    for spec in executor.specs:
        assert spec["mode"] == "lab_strategy_version"
        assert spec["from"] < spec["to"]
        assert spec["parameter_overrides"] in ({"period": 5}, {"period": 20})
    for fold in folds:
        window = fold["fold"]
        assert window["train_end"] < window["validation_start"] < window["test_start"]


def test_cscv_diagnostic_uses_only_supplied_oos_candidate_matrix() -> None:
    folds = [
        {
            "fold": {
                "test_start": f"2024-0{number}-01T00:00:00+00:00",
                "test_end": f"2024-0{number}-02T00:00:00+00:00",
            }
        }
        for number in range(1, 5)
    ]
    candidates = [{"period": number} for number in range(8)]
    statistics = _validation_statistics(
        pd.Series([0.01, -0.002, 0.005, 0.003]),
        candidates,
        folds,
        cscv_diagnostic=[[0.1 + candidate * 0.01 + fold * 0.001 for fold in range(4)] for candidate in range(8)],
        dataset_id="btc",
    )
    assert statistics["pbo"]["status"] == "computed"
    assert statistics["pbo"]["method"] == "legacy_cscv_style_oos_diagnostic"
    assert statistics["deflated_sharpe"]["status"] == "not_applicable"
