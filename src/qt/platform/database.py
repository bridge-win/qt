"""SQLAlchemy factories for platform-owned persistence."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from qt.platform.config import PlatformSettings

SessionFactory = sessionmaker[Session]


def normalize_database_url(url: str) -> str:
    """Select psycopg explicitly for bare PostgreSQL URLs."""

    bare_prefix = "postgresql://"
    if url.startswith(bare_prefix):
        return f"postgresql+psycopg://{url[len(bare_prefix):]}"
    return url


def create_platform_engine(settings: PlatformSettings) -> Engine:
    """Create the platform database engine from validated settings."""

    database_url = normalize_database_url(settings.database_url)
    connect_args: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(
        database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_session_factory(engine: Engine) -> SessionFactory:
    """Bind a non-expiring session factory to an engine."""

    return sessionmaker(bind=engine, expire_on_commit=False)
