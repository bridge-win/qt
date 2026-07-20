from __future__ import annotations

from sqlalchemy import text

from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, create_session_factory


def test_create_platform_engine_connects_to_sqlite() -> None:
    settings = PlatformSettings(database_url="sqlite+pysqlite:///:memory:", _env_file=None)

    engine = create_platform_engine(settings)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_create_session_factory_keeps_committed_values_available() -> None:
    engine = create_platform_engine(
        PlatformSettings(database_url="sqlite+pysqlite:///:memory:", _env_file=None)
    )

    session_factory = create_session_factory(engine)

    with session_factory() as session:
        assert session.expire_on_commit is False
