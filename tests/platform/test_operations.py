from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, create_session_factory
from qt.platform.models import Base
from qt.platform.operations import OperationsRepository
from qt.platform.schemas import WorkerStatus


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc))


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    settings = PlatformSettings(
        platform_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'operations.db'}",
        database_echo=False,
        command_lease_seconds=30,
        worker_stale_seconds=60,
        _env_file=None,  # type: ignore[call-arg]
    )
    database_engine = create_platform_engine(settings)
    Base.metadata.create_all(database_engine)
    yield database_engine
    Base.metadata.drop_all(database_engine)
    database_engine.dispose()


@pytest.fixture
def repository(engine: Engine, clock: MutableClock) -> OperationsRepository:
    return OperationsRepository(create_session_factory(engine), clock=clock)


def test_heartbeat_upsert_preserves_identity_and_creation_time(
    repository: OperationsRepository,
    clock: MutableClock,
) -> None:
    first = repository.record_heartbeat(
        role="trading",
        instance_id="worker-a",
        status=WorkerStatus.STARTING,
        version="1.0.0",
        details={"pid": 100},
    )
    clock.advance(seconds=5)

    updated = repository.record_heartbeat(
        role="trading",
        instance_id="worker-a",
        status=WorkerStatus.HEALTHY,
        version="1.0.1",
        details={"pid": 101},
    )

    assert updated.id == first.id
    assert updated.status is WorkerStatus.HEALTHY
    assert updated.version == "1.0.1"
    assert updated.details == {"pid": 101}
    assert updated.created_at == first.created_at
    assert updated.last_seen_at == clock.current
    assert updated.updated_at == clock.current


def test_list_heartbeats_filters_role_and_returns_detached_views(
    repository: OperationsRepository,
) -> None:
    trading = repository.record_heartbeat(
        role="trading",
        instance_id="worker-b",
        status=WorkerStatus.HEALTHY,
        version="1.0.0",
        details={},
    )
    scheduler = repository.record_heartbeat(
        role="scheduler",
        instance_id="worker-a",
        status=WorkerStatus.DEGRADED,
        version="1.0.0",
        details={},
    )

    assert repository.list_heartbeats(role="trading") == [trading]
    assert repository.list_heartbeats() == [scheduler, trading]
    with pytest.raises(ValidationError):
        trading.status = WorkerStatus.FAILED


def test_append_audit_and_list_newest_first(
    repository: OperationsRepository,
    clock: MutableClock,
) -> None:
    first = repository.append_audit(
        actor_id="operator",
        action="strategy.start",
        target_type="strategy",
        target_id="dca",
        correlation_id="request-1",
        details={"mode": "paper"},
    )
    clock.advance(seconds=1)
    second = repository.append_audit(
        actor_id="worker-a",
        action="strategy.started",
        target_type="strategy",
        target_id="dca",
        correlation_id="request-1",
        details={"fencing_token": 1},
    )
    clock.advance(seconds=1)
    other = repository.append_audit(
        actor_id="operator",
        action="job.start",
        target_type="job",
        target_id="backtest-1",
        correlation_id="request-2",
        details={},
    )

    assert repository.list_audit(limit=2) == [other, second]
    assert repository.list_audit(target_type="strategy", target_id="dca") == [second, first]
    with pytest.raises(ValidationError):
        first.action = "strategy.stop"


def test_audit_limit_must_be_positive(repository: OperationsRepository) -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        repository.list_audit(limit=0)


def test_operations_clock_must_be_aware(engine: Engine) -> None:
    repository = OperationsRepository(
        create_session_factory(engine),
        clock=lambda: datetime(2026, 7, 21, 8, 0),
    )

    with pytest.raises(ValueError, match="aware datetime"):
        repository.record_heartbeat(
            role="trading",
            instance_id="worker-a",
            status=WorkerStatus.HEALTHY,
            version="1.0.0",
            details={},
        )
