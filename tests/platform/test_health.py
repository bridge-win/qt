from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, event

from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, create_session_factory
from qt.platform.health import ExpectedWorker, HealthService, WorkerHealthStatus
from qt.platform.models import Base
from qt.platform.operations import OperationsRepository
from qt.platform.schemas import WorkerStatus


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc))


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    settings = PlatformSettings(
        platform_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'health.db'}",
        worker_stale_seconds=60,
        _env_file=None,  # type: ignore[call-arg]
    )
    database_engine = create_platform_engine(settings)
    Base.metadata.create_all(database_engine)
    yield database_engine
    Base.metadata.drop_all(database_engine)
    database_engine.dispose()


def test_readiness_executes_minimal_database_probe(engine: Engine) -> None:
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    session_factory = create_session_factory(engine)
    service = HealthService(
        session_factory,
        OperationsRepository(session_factory),
        stale_after_seconds=60,
    )

    try:
        report = service.readiness()
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert report.model_dump(mode="json") == {
        "status": "ready",
        "dependencies": {"database": {"status": "healthy"}},
    }
    assert [statement.strip().upper() for statement in statements] == ["SELECT 1"]


def test_readiness_redacts_database_failure(engine: Engine) -> None:
    def reject_query(
        _connection: object,
        _cursor: object,
        _statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        raise RuntimeError("postgresql://operator:secret@example.invalid/qt")

    event.listen(engine, "before_cursor_execute", reject_query)
    session_factory = create_session_factory(engine)
    service = HealthService(
        session_factory,
        OperationsRepository(session_factory),
        stale_after_seconds=60,
    )

    try:
        report = service.readiness()
    finally:
        event.remove(engine, "before_cursor_execute", reject_query)

    assert report.model_dump(mode="json") == {
        "status": "not_ready",
        "dependencies": {"database": {"status": "unavailable"}},
    }
    assert "secret" not in report.model_dump_json()


def test_worker_health_is_deterministic_for_fresh_stale_and_missing_workers(
    engine: Engine,
    clock: MutableClock,
) -> None:
    session_factory = create_session_factory(engine)
    operations = OperationsRepository(session_factory, clock=clock)
    clock.current -= timedelta(seconds=61)
    operations.record_heartbeat(
        role="scheduler",
        instance_id="scheduler-a",
        status=WorkerStatus.DEGRADED,
        version="1.0.0",
        details={"queue": "scheduled"},
    )
    clock.current += timedelta(seconds=1)
    operations.record_heartbeat(
        role="trading",
        instance_id="trading-a",
        status=WorkerStatus.HEALTHY,
        version="1.0.1",
        details={"mode": "paper"},
    )
    clock.current += timedelta(seconds=60)
    service = HealthService(
        session_factory,
        operations,
        expected_workers=(
            ExpectedWorker(role="trading", instance_id="trading-a"),
            ExpectedWorker(role="job", instance_id="job-a"),
            ExpectedWorker(role="scheduler", instance_id="scheduler-a"),
        ),
        stale_after_seconds=60,
        clock=clock,
    )

    report = service.worker_health()

    assert report.status == "degraded"
    assert [(worker.role, worker.instance_id) for worker in report.workers] == [
        ("job", "job-a"),
        ("scheduler", "scheduler-a"),
        ("trading", "trading-a"),
    ]
    missing, stale, healthy = report.workers
    assert missing.status is WorkerHealthStatus.MISSING
    assert missing.last_seen_at is None
    assert missing.reported_status is None
    assert stale.status is WorkerHealthStatus.STALE
    assert stale.reported_status is WorkerStatus.DEGRADED
    assert stale.last_seen_at == datetime(2026, 7, 21, 7, 58, 59, tzinfo=timezone.utc)
    assert healthy.status is WorkerHealthStatus.HEALTHY
    assert healthy.reported_status is WorkerStatus.HEALTHY
    assert healthy.last_seen_at == datetime(2026, 7, 21, 7, 59, tzinfo=timezone.utc)


def test_worker_health_requires_an_aware_clock(engine: Engine) -> None:
    session_factory = create_session_factory(engine)
    service = HealthService(
        session_factory,
        OperationsRepository(session_factory),
        expected_workers=(ExpectedWorker(role="trading", instance_id="trading-a"),),
        stale_after_seconds=60,
        clock=lambda: datetime(2026, 7, 21, 8, 0),
    )

    with pytest.raises(ValueError, match="aware datetime"):
        service.worker_health()
