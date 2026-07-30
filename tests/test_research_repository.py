from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qt.research.repository import ResearchRepository

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
    assert requested["status"] == "running"
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
