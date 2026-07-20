"""Alembic environment for the platform control-plane database."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context

from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine
from qt.platform.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    database_url = os.getenv("QT_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("QT_DATABASE_URL is required for Alembic migrations")
    return database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings = PlatformSettings(database_url=_database_url())
    engine = create_platform_engine(settings)

    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                transaction_per_migration=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
