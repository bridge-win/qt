"""Explicit database creation and additive, serialized schema migration."""

from __future__ import annotations

import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import event, insert, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from qt.persistence.schema import schema_versions


def create_engine(url: str, *, allow_sqlite: bool = False) -> Engine:
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("sqlite"):
        if not allow_sqlite:
            raise ValueError("SQLite is allowed only for explicit offline/test mode")
        options = {"poolclass": StaticPool} if ":memory:" in url else {}
        engine = sa_create_engine(url, connect_args={"check_same_thread": False}, **options)
        engine = engine.execution_options(schema_translate_map={"qt_v4": None})

        @event.listens_for(engine, "connect")
        def sqlite_settings(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")

        return engine
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("production database must use PostgreSQL/psycopg")
    return sa_create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5,
                            hide_parameters=True,
                            connect_args={"connect_timeout": 5,
                                          "options": "-c statement_timeout=5000 -c lock_timeout=3000"})


def migrate(engine: Engine) -> None:
    # Import table declarations before creating the shared schema. No application
    # startup calls this function: migrations belong to a one-shot deployment job.
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(814052631)"))
            connection.execute(text("CREATE SCHEMA IF NOT EXISTS qt_v4"))
        configuration = Config()
        configuration.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "head")
        if connection.execute(select(schema_versions.c.version).where(
            schema_versions.c.version == 1
        )).scalar_one_or_none() is None:
            connection.execute(insert(schema_versions).values(
                version=1, description="Immutable research, campaigns, risk and OMS state",
                created_ns=time.time_ns(),
            ))


def migrate_checkpoints(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        raise ValueError("persistent_graph_checkpoints_require_postgres")
    from langgraph.checkpoint.postgres import PostgresSaver

    url = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
