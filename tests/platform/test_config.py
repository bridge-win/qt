from __future__ import annotations

import pytest
from pydantic import ValidationError

from qt.platform.config import PlatformSettings


def test_production_requires_postgresql() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        PlatformSettings(
            env="production",
            database_url="sqlite+pysqlite:///:memory:",
            _env_file=None,
        )


def test_staging_requires_postgresql() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        PlatformSettings(
            env="staging",
            database_url="sqlite+pysqlite:///:memory:",
            _env_file=None,
        )


def test_rejects_unknown_environment() -> None:
    with pytest.raises(ValidationError):
        PlatformSettings(env="preview", _env_file=None)


def test_development_accepts_sqlite() -> None:
    settings = PlatformSettings(
        env="development",
        database_url="sqlite+pysqlite:///:memory:",
        _env_file=None,
    )

    assert settings.database_url.startswith("sqlite")


def test_environment_rejects_production_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_ENV", "production")
    monkeypatch.setenv("QT_DATABASE_URL", "sqlite+pysqlite:///:memory:")

    with pytest.raises(ValidationError, match="PostgreSQL"):
        PlatformSettings(_env_file=None)


def test_environment_accepts_production_psycopg_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+psycopg://operator:secret@database/qt_platform"
    monkeypatch.setenv("QT_ENV", "production")
    monkeypatch.setenv("QT_DATABASE_URL", database_url)

    settings = PlatformSettings(_env_file=None)

    assert settings.env == "production"
    assert settings.database_url == database_url
