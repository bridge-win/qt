from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import ORMExecuteState, Session

from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, create_session_factory
from qt.platform.leases import LeaseRepository
from qt.platform.models import Base, RuntimeLease, WorkerHeartbeat
from qt.platform.operations import OperationsRepository
from qt.platform.schemas import LeaseGrant, WorkerHeartbeatView, WorkerStatus

pytestmark = pytest.mark.integration


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _schema_url(database_url: str, schema_name: str) -> URL:
    url = make_url(database_url)
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema_name}"
    return url.set(query=query)


@pytest.fixture
def isolated_postgresql_url() -> Iterator[str]:
    database_url = os.getenv("QT_TEST_POSTGRES_URL")
    if database_url is None:
        pytest.skip("QT_TEST_POSTGRES_URL is not configured")

    schema_name = f"qt_leases_{uuid4().hex}"
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')

    try:
        yield _schema_url(database_url, schema_name).render_as_string(hide_password=False)
    finally:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        admin_engine.dispose()


@pytest.fixture
def postgresql_engine(isolated_postgresql_url: str) -> Iterator[Engine]:
    settings = PlatformSettings(
        platform_env="test",
        database_url=isolated_postgresql_url,
        database_echo=False,
        command_lease_seconds=30,
        worker_stale_seconds=60,
        _env_file=None,  # type: ignore[call-arg]
    )
    engine = create_platform_engine(settings)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _lease_repository_waiting_before_insert(
    engine: Engine,
    clock: Callable[[], datetime],
    barrier: Barrier,
) -> LeaseRepository:
    session_factory = create_session_factory(engine)

    def wait_before_lease_flush(
        session: Session,
        _flush_context: object,
        _instances: object,
    ) -> None:
        if session.info.get("waited_for_lease_flush"):
            return
        if not any(isinstance(instance, RuntimeLease) for instance in session.new):
            return
        session.info["waited_for_lease_flush"] = True
        barrier.wait(timeout=5)

    event.listen(session_factory, "before_flush", wait_before_lease_flush)
    return LeaseRepository(session_factory, clock=clock)


def _lease_repository_waiting_before_select(
    engine: Engine,
    clock: Callable[[], datetime],
    barrier: Barrier,
) -> LeaseRepository:
    session_factory = create_session_factory(engine)

    def wait_before_lease_select(execute_state: ORMExecuteState) -> None:
        if execute_state.session.info.get("waited_for_lease_select"):
            return
        execute_state.session.info["waited_for_lease_select"] = True
        barrier.wait(timeout=5)

    event.listen(session_factory, "do_orm_execute", wait_before_lease_select)
    return LeaseRepository(session_factory, clock=clock)


def test_concurrent_lease_ownership_and_monotonic_takeover(
    postgresql_engine: Engine,
) -> None:
    clock = MutableClock(datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc))
    insert_barrier = Barrier(2)

    def first_acquire(owner_id: str) -> LeaseGrant | None:
        repository = _lease_repository_waiting_before_insert(
            postgresql_engine,
            clock,
            insert_barrier,
        )
        return repository.acquire(
            resource_type="strategy",
            resource_id="dca",
            owner_id=owner_id,
            ttl_seconds=10,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        initial = [
            future.result(timeout=10)
            for future in [
                executor.submit(first_acquire, "worker-a"),
                executor.submit(first_acquire, "worker-b"),
            ]
        ]

    active_initial = [grant for grant in initial if grant is not None]
    assert len(active_initial) == 1
    assert active_initial[0].fencing_token == 1

    clock.advance(seconds=11)
    takeover_barrier = Barrier(2)

    def takeover(owner_id: str) -> LeaseGrant | None:
        repository = _lease_repository_waiting_before_select(
            postgresql_engine,
            clock,
            takeover_barrier,
        )
        return repository.acquire(
            resource_type="strategy",
            resource_id="dca",
            owner_id=owner_id,
            ttl_seconds=10,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        takeovers = [
            future.result(timeout=10)
            for future in [
                executor.submit(takeover, "worker-c"),
                executor.submit(takeover, "worker-d"),
            ]
        ]

    active_takeovers = [grant for grant in takeovers if grant is not None]
    assert len(active_takeovers) == 1
    assert active_takeovers[0].fencing_token == 2

    with create_session_factory(postgresql_engine)() as session:
        stored = session.scalar(select(RuntimeLease))
        row_count = session.scalar(select(func.count()).select_from(RuntimeLease))
    assert stored is not None
    assert row_count == 1
    assert stored.owner_id == active_takeovers[0].owner_id
    assert stored.fencing_token == 2

    stale = LeaseRepository(create_session_factory(postgresql_engine), clock=clock).renew(
        resource_type="strategy",
        resource_id="dca",
        owner_id=active_initial[0].owner_id,
        fencing_token=active_initial[0].fencing_token,
        ttl_seconds=10,
    )
    assert stale is None


def test_concurrent_first_heartbeat_is_upserted(
    postgresql_engine: Engine,
) -> None:
    clock = MutableClock(datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc))
    barrier = Barrier(2)
    winner_committed = Event()

    def heartbeat(status: WorkerStatus) -> WorkerHeartbeatView:
        session_factory = create_session_factory(postgresql_engine)

        def wait_before_heartbeat_flush(
            session: Session,
            _flush_context: object,
            _instances: object,
        ) -> None:
            if session.info.get("waited_for_heartbeat_flush"):
                return
            if not any(isinstance(instance, WorkerHeartbeat) for instance in session.new):
                return
            session.info["waited_for_heartbeat_flush"] = True
            barrier.wait(timeout=5)
            if status is WorkerStatus.HEALTHY:
                assert winner_committed.wait(timeout=5)

        event.listen(session_factory, "before_flush", wait_before_heartbeat_flush)
        view = OperationsRepository(session_factory, clock=clock).record_heartbeat(
            role="trading",
            instance_id="worker-a",
            status=status,
            version="1.0.0",
            details={"requested_status": status.value},
        )
        if status is WorkerStatus.STARTING:
            winner_committed.set()
        return view

    with ThreadPoolExecutor(max_workers=2) as executor:
        views = [
            future.result(timeout=10)
            for future in [
                executor.submit(heartbeat, WorkerStatus.STARTING),
                executor.submit(heartbeat, WorkerStatus.HEALTHY),
            ]
        ]

    assert views[0].id == views[1].id
    assert views[0].status is WorkerStatus.STARTING
    assert views[0].details == {"requested_status": "starting"}
    assert views[1].status is WorkerStatus.HEALTHY
    assert views[1].details == {"requested_status": "healthy"}
    with create_session_factory(postgresql_engine)() as session:
        stored = session.scalar(select(WorkerHeartbeat))
        row_count = session.scalar(select(func.count()).select_from(WorkerHeartbeat))
    assert stored is not None
    assert row_count == 1
    assert stored.status == WorkerStatus.HEALTHY.value
    assert stored.details == {"requested_status": "healthy"}
