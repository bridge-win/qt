from __future__ import annotations

import pytest
from pydantic import ValidationError

from qt.platform.config import PlatformSettings


def test_trading_environment_does_not_select_platform_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_ENV", "research")
    monkeypatch.setenv("QT_PLATFORM_ENV", "production")
    monkeypatch.delenv("QT_PLATFORM_ENV", raising=False)
    monkeypatch.delenv("QT_DATABASE_URL", raising=False)
    monkeypatch.delenv("QT_DATABASE_ECHO", raising=False)
    monkeypatch.delenv("QT_COMMAND_LEASE_SECONDS", raising=False)
    monkeypatch.delenv("QT_WORKER_STALE_SECONDS", raising=False)

    settings = PlatformSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.platform_env == "development"


def test_production_requires_postgresql() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        PlatformSettings(
            platform_env="production",
            database_url="sqlite+pysqlite:///:memory:",
            database_echo=False,
            command_lease_seconds=30,
            worker_stale_seconds=60,
            _env_file=None,  # type: ignore[call-arg]
        )


def test_staging_requires_postgresql() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        PlatformSettings(
            platform_env="staging",
            database_url="sqlite+pysqlite:///:memory:",
            database_echo=False,
            command_lease_seconds=30,
            worker_stale_seconds=60,
            _env_file=None,  # type: ignore[call-arg]
        )


def test_rejects_unknown_environment() -> None:
    with pytest.raises(ValidationError):
        PlatformSettings.model_validate(
            {
                "platform_env": "preview",
                "database_url": "sqlite+pysqlite:///:memory:",
                "database_echo": False,
                "command_lease_seconds": 30,
                "worker_stale_seconds": 60,
            }
        )


def test_development_accepts_sqlite() -> None:
    settings = PlatformSettings(
        platform_env="development",
        database_url="sqlite+pysqlite:///:memory:",
        database_echo=False,
        command_lease_seconds=30,
        worker_stale_seconds=60,
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.database_url.startswith("sqlite")


def test_environment_rejects_production_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_PLATFORM_ENV", "production")
    monkeypatch.setenv("QT_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("QT_DATABASE_ECHO", "false")
    monkeypatch.setenv("QT_COMMAND_LEASE_SECONDS", "30")
    monkeypatch.setenv("QT_WORKER_STALE_SECONDS", "60")

    with pytest.raises(ValidationError, match="PostgreSQL"):
        PlatformSettings(_env_file=None)  # type: ignore[call-arg]


def test_environment_accepts_production_psycopg_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+psycopg://operator:secret@database/qt_platform"
    monkeypatch.setenv("QT_PLATFORM_ENV", "production")
    monkeypatch.setenv("QT_DATABASE_URL", database_url)
    monkeypatch.setenv("QT_DATABASE_ECHO", "false")
    monkeypatch.setenv("QT_COMMAND_LEASE_SECONDS", "30")
    monkeypatch.setenv("QT_WORKER_STALE_SECONDS", "60")

    settings = PlatformSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.platform_env == "production"
    assert settings.database_url == database_url
