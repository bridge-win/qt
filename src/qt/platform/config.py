"""Platform-specific runtime settings."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PlatformEnvironment = Literal["development", "test", "staging", "production"]


class PlatformSettings(BaseSettings):
    """Settings for the production control-plane persistence boundary."""

    model_config = SettingsConfigDict(env_prefix="QT_", env_file=".env", extra="ignore")

    env: PlatformEnvironment = "development"
    database_url: str = "sqlite+pysqlite:///data/runtime/platform.db"
    database_echo: bool = False
    command_lease_seconds: int = Field(default=30, ge=5, le=3600)
    worker_stale_seconds: int = Field(default=60, ge=10, le=3600)

    @model_validator(mode="after")
    def require_postgresql_in_staging_and_production(self) -> PlatformSettings:
        if self.env in ("staging", "production") and not self.database_url.startswith(
            ("postgresql://", "postgresql+psycopg://")
        ):
            raise ValueError("staging and production platform storage must use PostgreSQL")
        return self
