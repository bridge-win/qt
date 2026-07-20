from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_TABLES = {
    "audit_events",
    "platform_commands",
    "runtime_leases",
    "worker_heartbeats",
}


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

    schema_name = f"qt_migration_{uuid4().hex}"
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')

    try:
        yield _schema_url(database_url, schema_name).render_as_string(hide_password=False)
    finally:
        admin_engine.dispose()
        cleanup_engine = create_engine(database_url)
        with cleanup_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        cleanup_engine.dispose()


def _alembic_config() -> Config:
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _run_offline_migration(database_url: str) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("QT_")}
    environment.update(
        {
            "QT_PLATFORM_ENV": "production",
            "QT_DATABASE_URL": database_url,
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from alembic import command; "
                "from alembic.config import Config; "
                "command.upgrade(Config('alembic.ini'), 'head', sql=True)"
            ),
        ],
        check=False,
        capture_output=True,
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
    )


def test_offline_migration_uses_validated_normalized_environment_url() -> None:
    invalid = _run_offline_migration("sqlite+pysqlite:///:memory:")

    assert invalid.returncode != 0
    assert "staging and production platform storage must use PostgreSQL" in invalid.stderr

    valid = _run_offline_migration("postgresql://qt:qt@127.0.0.1:55432/qt_test")

    assert valid.returncode == 0, valid.stderr
    assert "CREATE TABLE platform_commands" in valid.stdout


def test_audit_events_reject_update_and_delete_in_postgresql(
    isolated_postgresql_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_PLATFORM_ENV", "test")
    monkeypatch.setenv("QT_DATABASE_URL", isolated_postgresql_url)
    engine = create_engine(isolated_postgresql_url)
    command.upgrade(_alembic_config(), "head")
    event_id = uuid4()

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, actor_id, action, target_type, target_id, correlation_id, details
                ) VALUES (
                    :id, 'operator', 'strategy.start', 'strategy', 'dca', 'request-1',
                    CAST('{}' AS JSONB)
                )
                """
            ),
            {"id": event_id},
        )

    for statement in (
        text("UPDATE audit_events SET action = 'strategy.stop' WHERE id = :id"),
        text("DELETE FROM audit_events WHERE id = :id"),
    ):
        with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(statement, {"id": event_id})
            connection.rollback()
            assert connection.execute(
                text("SELECT COUNT(*) FROM audit_events WHERE id = :id"),
                {"id": event_id},
            ).scalar_one() == 1

    engine.dispose()


def _constraint_names(engine: Engine, table_name: str) -> set[str]:
    inspector = inspect(engine)
    names = {
        constraint["name"]
        for constraint in inspector.get_check_constraints(table_name)
        if constraint["name"] is not None
    }
    names.update(
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint["name"] is not None
    )
    return names


def test_upgrade_from_empty_schema_and_downgrade_cleanly(
    isolated_postgresql_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_PLATFORM_ENV", "test")
    monkeypatch.setenv("QT_DATABASE_URL", isolated_postgresql_url)
    engine = create_engine(isolated_postgresql_url)
    assert inspect(engine).get_table_names() == []

    command.upgrade(_alembic_config(), "head")

    inspector = inspect(engine)
    assert set(inspector.get_table_names()) > PLATFORM_TABLES
    assert {
        "uq_command_owner_idempotency",
        "ck_platform_commands_command_type",
        "ck_platform_commands_status",
        "ck_command_attempts_non_negative",
        "ck_command_max_attempts_positive",
    } <= _constraint_names(engine, "platform_commands")
    assert {
        "uq_runtime_lease_resource",
        "ck_runtime_lease_fencing_token_positive",
    } <= _constraint_names(engine, "runtime_leases")
    assert {
        "uq_worker_heartbeat_identity",
        "ck_worker_heartbeats_status",
    } <= _constraint_names(engine, "worker_heartbeats")
    assert "ix_command_claimable" in {
        index["name"] for index in inspector.get_indexes("platform_commands")
    }
    with engine.connect() as connection:
        native_enums = connection.execute(
            text(
                """
                SELECT typname
                FROM pg_type
                WHERE typnamespace = current_schema()::regnamespace
                  AND typtype = 'e'
                """
            )
        ).scalars()
        assert list(native_enums) == []

    command.downgrade(_alembic_config(), "base")

    assert PLATFORM_TABLES.isdisjoint(inspect(engine).get_table_names())
    with engine.connect() as connection:
        remaining_enums = connection.execute(
            text(
                """
                SELECT typname
                FROM pg_type
                WHERE typnamespace = current_schema()::regnamespace
                  AND typtype = 'e'
                """
            )
        ).scalars()
        assert list(remaining_enums) == []
        assert connection.execute(
            text("SELECT to_regprocedure('prevent_audit_event_mutation()')")
        ).scalar_one() is None
    engine.dispose()
