from __future__ import annotations

from pathlib import Path

from qt.research.repository import ResearchRepository
from qt.research.worker import ResearchWorker


def _spec() -> dict[str, object]:
    return {
        "dataset_id": "okx-btcusdt-1h",
        "mode": "template",
        "strategy_id": "buy_and_hold",
        "strategy_params": {},
        "validation_profile": "quick",
        "assumptions": {
            "initial_cash": 10_000,
            "fee_bps": 10,
            "slippage_bps": 5,
        },
        "seed": 7,
    }


def test_worker_persists_progress_and_completed_result(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.sqlite3")
    queued = repository.enqueue(_spec())
    observed: list[tuple[str, int]] = []

    def execute(
        spec: dict[str, object],
        progress: object,
        cancelled: object,
    ) -> dict[str, object]:
        del spec, cancelled
        assert callable(progress)
        progress("simulation", 45)
        observed.append(("simulation", 45))
        return {"run_id": "run-123", "verdict": {"status": "fragile"}}

    worker = ResearchWorker(repository, worker_id="worker-1", executor=execute)

    assert worker.run_once() is True
    completed = repository.get_job(queued["job_id"])
    assert completed["status"] == "complete"
    assert completed["progress"] == 100
    assert completed["result"]["run_id"] == "run-123"
    assert observed == [("simulation", 45)]


def test_worker_honors_cancellation_before_execution(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.sqlite3")
    queued = repository.enqueue(_spec())
    repository.request_cancel(queued["job_id"])
    called = False

    def execute(
        spec: dict[str, object],
        progress: object,
        cancelled: object,
    ) -> dict[str, object]:
        nonlocal called
        del spec, progress, cancelled
        called = True
        return {"run_id": "not-used"}

    worker = ResearchWorker(repository, worker_id="worker-1", executor=execute)

    assert worker.run_once() is False
    assert called is False
    assert repository.get_job(queued["job_id"])["status"] == "cancelled"
