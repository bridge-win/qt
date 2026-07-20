from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import ORMExecuteState

from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, create_session_factory
from qt.platform.leases import LeaseRepository
from qt.platform.models import Base, RuntimeLease


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
        database_url=f"sqlite+pysqlite:///{tmp_path / 'leases.db'}",
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
def lease_repository(engine: Engine, clock: MutableClock) -> LeaseRepository:
    return LeaseRepository(create_session_factory(engine), clock=clock)


def test_first_acquisition_starts_fencing_at_one(
    lease_repository: LeaseRepository,
    clock: MutableClock,
) -> None:
    grant = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=10,
    )

    assert grant is not None
    assert grant.fencing_token == 1
    assert grant.expires_at == clock.current + timedelta(seconds=10)


def test_same_owner_renews_active_lease_without_incrementing_token(
    lease_repository: LeaseRepository,
    clock: MutableClock,
) -> None:
    first = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=10,
    )
    assert first is not None
    clock.advance(seconds=5)

    renewed = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=20,
    )

    assert renewed is not None
    assert renewed.fencing_token == first.fencing_token
    assert renewed.expires_at == clock.current + timedelta(seconds=20)


def test_different_owner_cannot_acquire_active_lease(
    lease_repository: LeaseRepository,
) -> None:
    assert lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=10,
    ) is not None

    assert lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-b",
        ttl_seconds=10,
    ) is None


def test_takeover_increments_fencing_token(
    lease_repository: LeaseRepository,
    clock: MutableClock,
) -> None:
    first = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=10,
    )
    assert first is not None
    clock.advance(seconds=11)

    second = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-b",
        ttl_seconds=10,
    )

    assert second is not None
    assert second.fencing_token == first.fencing_token + 1


def test_expired_reacquisition_by_same_owner_increments_fencing_token(
    lease_repository: LeaseRepository,
    clock: MutableClock,
) -> None:
    first = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=10,
    )
    assert first is not None
    clock.advance(seconds=10)

    reacquired = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=10,
    )

    assert reacquired is not None
    assert reacquired.fencing_token == first.fencing_token + 1


def test_renew_requires_active_matching_owner_and_token(
    lease_repository: LeaseRepository,
    clock: MutableClock,
) -> None:
    grant = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=10,
    )
    assert grant is not None

    assert lease_repository.renew(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-b",
        fencing_token=grant.fencing_token,
        ttl_seconds=10,
    ) is None
    assert lease_repository.renew(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        fencing_token=grant.fencing_token + 1,
        ttl_seconds=10,
    ) is None

    clock.advance(seconds=10)
    assert lease_repository.renew(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        fencing_token=grant.fencing_token,
        ttl_seconds=10,
    ) is None


def test_release_expires_row_and_preserves_fencing_history(
    lease_repository: LeaseRepository,
    engine: Engine,
    clock: MutableClock,
) -> None:
    grant = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=10,
    )
    assert grant is not None

    assert lease_repository.release(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-b",
        fencing_token=grant.fencing_token,
    ) is False
    assert lease_repository.release(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        fencing_token=grant.fencing_token,
    ) is True

    with create_session_factory(engine)() as session:
        stored = session.scalar(select(RuntimeLease))
        row_count = session.scalar(select(func.count()).select_from(RuntimeLease))
    assert stored is not None
    assert row_count == 1
    assert stored.expires_at == clock.current
    assert stored.fencing_token == grant.fencing_token

    takeover = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-b",
        ttl_seconds=10,
    )
    assert takeover is not None
    assert takeover.fencing_token == grant.fencing_token + 1


def test_expiry_decision_and_new_expiry_are_sampled_after_resource_lock(
    engine: Engine,
    clock: MutableClock,
) -> None:
    initial = LeaseRepository(create_session_factory(engine), clock=clock).acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=10,
    )
    assert initial is not None
    clock.advance(seconds=9)
    session_factory = create_session_factory(engine)

    def advance_clock(_execute_state: ORMExecuteState) -> None:
        clock.advance(seconds=2)

    event.listen(session_factory, "do_orm_execute", advance_clock, once=True)
    repository = LeaseRepository(session_factory, clock=clock)

    grant = repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-b",
        ttl_seconds=10,
    )

    assert grant is not None
    assert grant.owner_id == "worker-b"
    assert grant.fencing_token == initial.fencing_token + 1
    assert grant.expires_at == clock.current + timedelta(seconds=10)


@pytest.mark.parametrize("ttl_seconds", [0, -1])
def test_lease_ttl_must_be_positive(
    lease_repository: LeaseRepository,
    ttl_seconds: int,
) -> None:
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        lease_repository.acquire(
            resource_type="strategy",
            resource_id="dca",
            owner_id="worker-a",
            ttl_seconds=ttl_seconds,
        )
