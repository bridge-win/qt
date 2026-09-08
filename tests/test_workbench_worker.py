from __future__ import annotations

from pathlib import Path

import pytest

import qt.workbench.worker as worker_module
from qt.workbench.resources import CapacityBlockedError, ResourcePolicy
from qt.workbench.worker import WorkbenchWorker


class _Operations:
    def __init__(self) -> None:
        self.spec: dict[str, object] | None = None

    def execute(
        self,
        spec: dict[str, object],
        progress: object,
        cancelled: object,
    ) -> dict[str, object]:
        assert callable(progress)
        assert callable(cancelled)
        self.spec = spec
        return {"operation": "ok"}


class _Lab:
    def __init__(self) -> None:
        self.spec: dict[str, object] | None = None

    def execute(
        self,
        spec: dict[str, object],
        progress: object,
        cancelled: object,
    ) -> dict[str, object]:
        assert callable(progress)
        assert callable(cancelled)
        self.spec = spec
        return {"lab": "ok"}


class _Native:
    def __init__(self) -> None:
        self.spec: dict[str, object] | None = None

    def execute(
        self,
        spec: dict[str, object],
        progress: object,
        cancelled: object,
    ) -> dict[str, object]:
        assert callable(progress)
        assert callable(cancelled)
        self.spec = spec
        return {"run_id": "native", "artifacts": []}


class _Reports:
    def __init__(self) -> None:
        self.result: dict[str, object] | None = None

    def publish_result(self, result: dict[str, object]) -> dict[str, object]:
        self.result = result
        return {
            **result,
            "report_publication": {"status": "not_configured", "bucket": None},
        }


class _Catalog:
    def __init__(self, rows: int = 100) -> None:
        self.rows = rows
        self.dataset_ids: list[str] = []

    def get(self, dataset_id: str) -> dict[str, object]:
        self.dataset_ids.append(dataset_id)
        return {"dataset_id": dataset_id, "status": "ready", "rows": self.rows}


def _dispatcher() -> tuple[WorkbenchWorker, _Operations, _Lab, _Native, _Reports]:
    worker = object.__new__(WorkbenchWorker)
    operations = _Operations()
    lab = _Lab()
    native = _Native()
    reports = _Reports()
    worker.operations_service = operations
    worker.lab_service = lab
    worker.native_executor = native
    worker.report_publisher = reports
    worker.dataset_catalog = _Catalog()
    worker.resource_policy = ResourcePolicy(min_available_memory_mib=0)
    return worker, operations, lab, native, reports


def _progress(_stage: str, _percent: int) -> None:
    return None


def _not_cancelled() -> bool:
    return False


@pytest.mark.parametrize("job_type", ["data_import", "data_sync"])
def test_dispatches_shared_operation_types(job_type: str) -> None:
    worker, operations, _lab, _native, _reports = _dispatcher()

    assert worker._execute(
        {"job_type": job_type, "payload": {}, "_job_id": "job-1"},
        _progress,
        _not_cancelled,
    ) == {"operation": "ok"}
    assert operations.spec is not None
    assert operations.spec["_job_id"] == "job-1"


@pytest.mark.parametrize("job_type", ["lab_optimization", "lab_validation"])
def test_dispatches_shared_lab_types_with_transient_job_id(job_type: str) -> None:
    worker, _operations, lab, _native, _reports = _dispatcher()

    assert worker._execute(
        {
            "job_type": job_type,
            "payload": {"experiment": {"dataset_id": "btc"}},
            "_job_id": "job-1",
        },
        _progress,
        _not_cancelled,
    ) == {"lab": "ok"}
    assert lab.spec is not None
    assert lab.spec["lab_job_id"] == "job-1"
    assert "_job_id" not in lab.spec


def test_dispatches_native_experiment_without_transient_job_id() -> None:
    worker, _operations, _lab, native, reports = _dispatcher()

    assert worker._execute(
        {"job_type": "native_experiment", "dataset_id": "btc", "_job_id": "job-1"},
        _progress,
        _not_cancelled,
    ) == {
        "run_id": "native",
        "artifacts": [],
        "report_publication": {"status": "not_configured", "bucket": None},
    }
    assert native.spec == {"job_type": "native_experiment", "dataset_id": "btc"}
    assert reports.result == {"run_id": "native", "artifacts": []}


def test_plugin_result_is_not_presented_as_temporally_verified() -> None:
    worker, _operations, _lab, _native, _reports = _dispatcher()

    result = worker._execute(
        {
            "job_type": "native_experiment",
            "dataset_id": "btc",
            "lab_strategy_version": {"content": {"mode": "plugin"}},
        },
        _progress,
        _not_cancelled,
    )

    assert result["temporal_integrity"] == {
        "status": "unverified",
        "strategy_mode": "plugin",
        "verified_leaderboard_eligible": False,
        "reason": (
            "container isolation prevents host/network access but does not prove that "
            "plugin code avoided future rows"
        ),
    }


def test_dispatch_rejects_unknown_job_type() -> None:
    worker, _operations, _lab, _native, _reports = _dispatcher()

    with pytest.raises(ValueError, match="unsupported workbench job_type"):
        worker._execute({"job_type": "live_order"}, _progress, _not_cancelled)


@pytest.mark.parametrize(
    "spec",
    [
        {"job_type": "native_experiment", "dataset_id": "btc", "estimated_rows": 0},
        {
            "job_type": "lab_optimization",
            "payload": {"experiment": {"dataset_id": "btc"}},
            "_job_id": "job-1",
            "estimated_rows": 0,
        },
        {
            "job_type": "lab_validation",
            "payload": {"experiment": {"dataset_id": "btc"}},
            "_job_id": "job-1",
            "estimated_rows": 0,
        },
    ],
)
def test_worker_refuses_catalog_rows_above_capacity_before_executor(
    spec: dict[str, object],
) -> None:
    worker, _operations, lab, native, _reports = _dispatcher()
    worker.dataset_catalog = _Catalog(rows=2)
    worker.resource_policy = ResourcePolicy(max_estimated_rows=1, min_available_memory_mib=0)

    with pytest.raises(CapacityBlockedError, match="estimated rows exceed"):
        worker._execute(spec, _progress, _not_cancelled)

    assert native.spec is None
    assert lab.spec is None
    assert worker.dataset_catalog.dataset_ids == ["btc"]


def test_worker_refuses_low_memory_before_native_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, _operations, _lab, native, _reports = _dispatcher()
    worker.resource_policy = ResourcePolicy(min_available_memory_mib=512)
    monkeypatch.setattr(worker_module, "available_memory_mib", lambda: 511)

    with pytest.raises(CapacityBlockedError, match="insufficient free memory"):
        worker._execute(
            {"job_type": "native_experiment", "dataset_id": "btc"},
            _progress,
            _not_cancelled,
        )

    assert native.spec is None


def test_worker_injects_publisher_and_lab_artifacts_are_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _ConstructedNative:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def execute(
            self, _spec: object, _progress: object, _cancelled: object
        ) -> dict[str, object]:
            return {"run_id": "lab-run", "artifacts": [{"name": "fills.json"}]}

    class _PublishingReports:
        def __init__(self) -> None:
            self.result: dict[str, object] | None = None

        def publish_result(self, result: dict[str, object]) -> dict[str, object]:
            self.result = result
            return {**result, "report_publication": {"status": "published"}}

    from qt.nautilus import executor as executor_module

    reports = _PublishingReports()
    monkeypatch.setattr(executor_module, "NautilusResearchExecutor", _ConstructedNative)
    monkeypatch.setattr(worker_module, "R2ReportPublisher", lambda *_args, **_kwargs: reports)
    worker = WorkbenchWorker(
        parquet_root=tmp_path / "parquet",
        state_root=tmp_path / "state",
        worker_id="worker",
    )

    assert worker.lab_service.result_publisher is reports
    result = worker.lab_service._execute_native({}, _progress, _not_cancelled)
    assert reports.result == {"run_id": "lab-run", "artifacts": [{"name": "fills.json"}]}
    assert result["report_publication"] == {"status": "published"}
