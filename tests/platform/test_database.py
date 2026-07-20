from __future__ import annotations

from sqlalchemy import text

from qt.platform.config import PlatformSettings
from qt.platform.database import (
    create_platform_engine,
    create_session_factory,
    normalize_database_url,
)


def test_create_platform_engine_normalizes_bare_postgresql_url() -> None:
    settings = PlatformSettings(
        platform_env="test",
        database_url="postgresql://qt:qt@127.0.0.1:55432/qt_test",
        _env_file=None,  # type: ignore[call-arg]
    )

    engine = create_platform_engine(settings)

    assert engine.url.drivername == "postgresql+psycopg"
    assert normalize_database_url(settings.database_url) == engine.url.render_as_string(
        hide_password=False
    )
    engine.dispose()


def test_create_platform_engine_connects_to_sqlite() -> None:
    settings = PlatformSettings(
        platform_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        _env_file=None,  # type: ignore[call-arg]
    )

    engine = create_platform_engine(settings)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_create_session_factory_keeps_committed_values_available() -> None:
    engine = create_platform_engine(
        PlatformSettings(
            platform_env="test",
            database_url="sqlite+pysqlite:///:memory:",
            _env_file=None,  # type: ignore[call-arg]
        )
    )

    session_factory = create_session_factory(engine)

    with session_factory() as session:
        assert session.expire_on_commit is False
