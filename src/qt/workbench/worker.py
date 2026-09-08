"""Single-process FIFO dispatcher for the durable workbench research queue.

The API only enqueues.  This process owns one common queue's native research,
lab, and data operations under one attempt lease and one process lock.  It
never enables live trading or sends an order.
"""

from __future__ import annotations

import fcntl
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from qt.lab.persistence import LabRepository
from qt.lab.service import LabService, ParquetTimelineProvider
from qt.research.datasets import DatasetCatalog
from qt.research.repository import ResearchRepository
from qt.research.worker import ResearchWorker
from qt.workbench.operations import OperationsService
from qt.workbench.plugin_runtime import IsolatedPluginRuntime
from qt.workbench.reports import R2ReportPublisher, R2ReportSettings
from qt.workbench.resources import ResourcePolicy, available_memory_mib

if TYPE_CHECKING:
    from qt.nautilus.executor import NautilusResearchExecutor


class WorkbenchWorker:
    """Run one FIFO job at a time against shared SQLite WAL."""

    def __init__(
        self,
        *,
        parquet_root: Path,
        state_root: Path,
        worker_id: str,
    ) -> None:
        database = state_root / "research.sqlite3"
        self.research_repository = ResearchRepository(database, queue_limit=20)
        self.dataset_catalog = DatasetCatalog(parquet_root)
        self.resource_policy = ResourcePolicy()
        # Importing the executor only inside the worker preserves Python 3.10
        # API/catalog inspection on hosts without the optional native runtime.
        from qt.nautilus.executor import NautilusResearchExecutor

        artifacts = state_root / "artifacts"
        self.plugin_runtime = IsolatedPluginRuntime(
            parquet_root=parquet_root,
            artifact_root=artifacts,
        )
        self.native_executor: NautilusResearchExecutor = NautilusResearchExecutor(
            parquet_root,
            artifacts,
            plugin_runtime=self.plugin_runtime,
        )
        self.report_publisher = R2ReportPublisher(
            R2ReportSettings.from_environment(), artifact_root=artifacts
        )
        self.lab_service = LabService(
            LabRepository(database),
            self.native_executor,
            timeline_provider=ParquetTimelineProvider(parquet_root),
            research_repository=self.research_repository,
            result_publisher=self.report_publisher,
        )
        self.operations_service = OperationsService(
            research_repository=self.research_repository,
            state_root=state_root,
            parquet_root=parquet_root,
        )
        self.research_worker = ResearchWorker(
            self.research_repository,
            worker_id=worker_id,
            executor=self._execute,
        )
        self._lock_path = state_root / "workbench-worker.lock"

    def run_next(self) -> bool:
        """Run one FIFO job under a process-wide lock."""

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            try:
                return self._run_next()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _run_next(self) -> bool:
        before = self.research_repository.list_runs(limit=1)
        if not self.research_worker.run_once():
            return False
        after = self.research_repository.list_runs(limit=1)
        if after and after != before:
            self.lab_service.import_native_result(after[0])
        return True

    def _execute(
        self,
        spec: dict[str, object],
        progress: object,
        cancelled: object,
    ) -> dict[str, object]:
        """Dispatch a claimed job; lifecycle ownership stays in ResearchWorker."""

        if not callable(progress) or not callable(cancelled):
            raise TypeError("worker progress and cancellation callbacks are required")
        job_type = spec.get("job_type")
        if job_type in {"data_import", "data_sync"}:
            return self.operations_service.execute(spec, progress, cancelled)
        if job_type in {"lab_optimization", "lab_validation"}:
            self._admit_native_work(spec)
            lab_spec = dict(spec)
            job_id = lab_spec.pop("_job_id", None)
            if not isinstance(job_id, str):
                raise ValueError("lab job has no transient queue identifier")
            lab_spec["lab_job_id"] = job_id
            return self.lab_service.execute(lab_spec, progress, cancelled)
        if job_type == "native_experiment":
            self._admit_native_work(spec)
            native_spec = dict(spec)
            native_spec.pop("_job_id", None)
            result = self.native_executor.execute(native_spec, progress, cancelled)
            if _is_plugin_version(native_spec):
                result = {
                    **result,
                    "temporal_integrity": {
                        "status": "unverified",
                        "strategy_mode": "plugin",
                        "verified_leaderboard_eligible": False,
                        "reason": (
                            "container isolation prevents host/network access but does not prove that "
                            "plugin code avoided future rows"
                        ),
                    },
                }
            return self.report_publisher.publish_result(result)
        raise ValueError(f"unsupported workbench job_type: {job_type!r}")

    def _admit_native_work(self, spec: Mapping[str, object]) -> None:
        """Recheck resource capacity from the current read-only dataset catalog."""

        dataset = self.dataset_catalog.get(_dataset_id(spec))
        if dataset.get("status") != "ready":
            raise ValueError(f"dataset is not ready: {dataset['dataset_id']}")
        self.resource_policy.admit(
            {"estimated_rows": dataset.get("rows", 0)},
            available_memory_mib=available_memory_mib(),
        )


def _is_plugin_version(spec: dict[str, object]) -> bool:
    version = spec.get("lab_strategy_version")
    if not isinstance(version, dict):
        return False
    content = version.get("content")
    return isinstance(content, dict) and content.get("mode") == "plugin"


def _dataset_id(spec: Mapping[str, object]) -> str:
    """Extract the persisted dataset identity; lab requests wrap it in payload."""

    if spec.get("job_type") == "native_experiment":
        dataset_id = spec.get("dataset_id")
    else:
        payload = spec.get("payload")
        experiment = payload.get("experiment") if isinstance(payload, Mapping) else None
        dataset_id = experiment.get("dataset_id") if isinstance(experiment, Mapping) else None
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValueError("native job has no dataset_id")
    return dataset_id
