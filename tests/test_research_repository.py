from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qt.research.repository import IdempotencyConflictError, ResearchRepository

UTC = timezone.utc


def _spec(strategy_id: str = "sma_crossover") -> dict[str, object]:
    return {
        "dataset_id": "okx-btcusdt-1h",
        "mode": "template",
        "template": {"strategy_id": strategy_id, "parameters": {}},
        "validation_profile": "quick",
        "assumptions": {
            "initial_cash": 10_000,
            "fee_bps": 10,
            "slippage_bps": 5,
        },
        "seed": 7,
    }


def test_repository_persists_and_recovers_stale_jobs(tmp_path: Path) -> None:
    now = datetime(2026, 7, 30, tzinfo=UTC)
    repository = ResearchRepository(tmp_path / "research.sqlite3", clock=lambda: now)
    queued = repository.enqueue(_spec())

    claimed = repository.claim_next("worker-1")
    assert claimed is not None
    assert claimed["job_id"] == queued["job_id"]
    assert claimed["status"] == "running"

    later = now + timedelta(minutes=30)
    recovered = ResearchRepository(
        tmp_path / "research.sqlite3",
        clock=lambda: later,
    ).recover_stale(stale_after=timedelta(minutes=10))

    assert recovered == 1
    assert repository.get_job(queued["job_id"])["status"] == "queued"
    reclaimed = repository.claim_next("worker-2")
    assert reclaimed is not None
    assert reclaimed["attempts"] == 2


def test_repository_cancels_queued_and_running_jobs(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.sqlite3")
    queued = repository.enqueue(_spec())
    cancelled = repository.request_cancel(queued["job_id"])
    assert cancelled["status"] == "cancelled"

    running = repository.enqueue(_spec("ema_crossover"))
    repository.claim_next("worker")
    requested = repository.request_cancel(running["job_id"])
    assert requested["status"] == "cancelling"
    assert requested["v3_status"] == "cancelling"
    assert requested["cancel_requested"] is True
    assert repository.is_cancel_requested(running["job_id"]) is True


def test_repository_enforces_queue_limit(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.sqlite3", queue_limit=2)
    repository.enqueue(_spec())
    repository.enqueue(_spec("ema_crossover"))

    try:
        repository.enqueue(_spec("buy_and_hold"))
    except ValueError as error:
        assert str(error) == "backtest queue is full: 2 queued/running"
    else:
        raise AssertionError("expected queue limit rejection")


def test_cancel_requested_job_cannot_publish_a_run(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.sqlite3")
    queued = repository.enqueue(_spec("buy_and_hold"))
    claimed = repository.claim_next("worker-1")
    assert claimed is not None
    job_id = str(queued["job_id"])
    repository.request_cancel(job_id)

    with pytest.raises(ValueError, match="cannot complete"):
        repository.complete(job_id, {"run_id": "a" * 32})

    assert repository.list_runs() == []


def test_idempotent_enqueue_is_transactional_under_concurrent_retries(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.sqlite3")

    def submit() -> tuple[dict[str, object], bool]:
        return repository.enqueue_idempotent(_spec(), idempotency_key="browser-retry-1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _unused: submit(), range(2)))

    assert {created for _job, created in (first, second)} == {True, False}
    assert first[0]["job_id"] == second[0]["job_id"]
    assert repository.queue_counts()["queued"] == 1


def test_idempotency_key_rejects_different_payload(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.sqlite3")
    repository.enqueue_idempotent(_spec(), idempotency_key="same-key")

    with pytest.raises(IdempotencyConflictError, match="different request"):
        repository.enqueue_idempotent(_spec("ema_crossover"), idempotency_key="same-key")


def test_stale_attempt_cannot_complete_after_recovery_and_reclaim(tmp_path: Path) -> None:
    now = datetime(2026, 7, 30, tzinfo=UTC)
    repository = ResearchRepository(tmp_path / "research.sqlite3", clock=lambda: now)
    queued = repository.enqueue(_spec())
    first = repository.claim_next("worker-1")
    assert first is not None
    first_attempt = str(first["attempt_id"])

    later = now + timedelta(minutes=30)
    recovered = ResearchRepository(tmp_path / "research.sqlite3", clock=lambda: later)
    assert recovered.recover_stale(stale_after=timedelta(minutes=10)) == 1
    second = recovered.claim_next("worker-2")
    assert second is not None
    second_attempt = str(second["attempt_id"])
    assert first_attempt != second_attempt

    with pytest.raises(ValueError, match="cannot complete"):
        recovered.complete(str(queued["job_id"]), {"run_id": "stale"}, attempt_id=first_attempt)
    completed = recovered.complete(
        str(queued["job_id"]), {"run_id": "fresh"}, attempt_id=second_attempt
    )
    assert completed["status"] == "complete"
    assert [run["run_id"] for run in recovered.list_runs()] == ["fresh"]


def test_second_worker_cannot_claim_while_native_attempt_is_running(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.sqlite3")
    first = repository.enqueue(_spec())
    repository.enqueue(_spec("ema_crossover"))
    assert repository.claim_next("worker-1") is not None
    assert repository.claim_next("worker-2") is None
    assert repository.get_job(str(first["job_id"]))["status"] == "running"


def test_common_queue_normalizes_job_types_and_exposes_v3_status(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.sqlite3")
    legacy = repository.enqueue({"kind": "dataset_sync", "dataset_id": "btc-1h"})
    optimization = repository.enqueue(
        {"job_type": "lab_optimization", "strategy_version_id": "version-1"}
    )

    assert legacy["job_type"] == "data_sync"
    assert optimization["job_type"] == "lab_optimization"
    assert [job["job_id"] for job in repository.list_jobs(job_types={"data_sync"})] == [
        legacy["job_id"]
    ]

    claimed = repository.claim_next("worker")
    assert claimed is not None
    completed = repository.complete(
        str(claimed["job_id"]),
        {"run_id": "sync-result"},
        attempt_id=str(claimed["attempt_id"]),
    )
    assert completed["status"] == "complete"  # v2 compatibility
    assert completed["v3_status"] == "succeeded"
