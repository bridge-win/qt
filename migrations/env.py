"""Alembic environment for the platform control-plane database."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context

from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, normalize_database_url
from qt.platform.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _platform_settings() -> PlatformSettings:
    if os.getenv("QT_DATABASE_URL") is None:
        raise RuntimeError("QT_DATABASE_URL is required for Alembic migrations")
    settings = PlatformSettings(_env_file=None)  # type: ignore[call-arg]
    return settings.model_copy(
        update={"database_url": normalize_database_url(settings.database_url)}
    )


def run_migrations_offline() -> None:
    settings = _platform_settings()
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings = _platform_settings()
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
