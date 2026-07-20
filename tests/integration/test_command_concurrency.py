from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url

from qt.platform.commands import CommandRepository
from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, create_session_factory
from qt.platform.models import Base
from qt.platform.schemas import CommandType

pytestmark = pytest.mark.integration


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

    schema_name = f"qt_commands_{uuid4().hex}"
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


def test_concurrent_enqueue_replays_committed_command(postgresql_engine: Engine) -> None:
    barrier = Barrier(2)

    def enqueue() -> UUID:
        repository = CommandRepository(create_session_factory(postgresql_engine))
        barrier.wait()
        return repository.enqueue(
            owner_id="operator",
            command_type=CommandType.START,
            target="dca",
            payload={"mode": "paper"},
            idempotency_key="concurrent-start-1",
        ).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(enqueue) for _ in range(2)]
        command_ids = [future.result(timeout=10) for future in futures]

    assert command_ids[0] == command_ids[1]
    assert len(CommandRepository(create_session_factory(postgresql_engine)).list_recent()) == 1


def test_two_workers_claim_one_command_once(postgresql_engine: Engine) -> None:
    command_id = CommandRepository(create_session_factory(postgresql_engine)).enqueue(
        owner_id="operator",
        command_type=CommandType.NOOP,
        target="worker",
        payload={},
        idempotency_key="concurrent-noop-1",
    ).id
    barrier = Barrier(2)

    def claim(worker_id: str) -> tuple[object | None, int | None]:
        repository = CommandRepository(create_session_factory(postgresql_engine))
        barrier.wait()
        claimed = repository.claim_next(worker_id=worker_id, lease_seconds=30)
        if claimed is None:
            return None, None
        return claimed.id, claimed.attempts

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, f"worker-{index}") for index in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert sorted(attempts for _, attempts in results if attempts is not None) == [1]
    assert [claimed_id for claimed_id, _ in results if claimed_id is not None] == [command_id]
    assert sum(claimed_id is None for claimed_id, _ in results) == 1

    stored = CommandRepository(create_session_factory(postgresql_engine)).get(command_id)
    assert stored is not None
    assert stored.attempts == 1
