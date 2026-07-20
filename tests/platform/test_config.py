from __future__ import annotations

import pytest
from pydantic import ValidationError

from qt.platform.config import PlatformSettings


def test_production_requires_postgresql() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        PlatformSettings(env="production", database_url="sqlite+pysqlite:///:memory:")


def test_development_accepts_sqlite() -> None:
    settings = PlatformSettings(
        env="development",
        database_url="sqlite+pysqlite:///:memory:",
    )

    assert settings.database_url.startswith("sqlite")
