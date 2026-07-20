"""Read-only Phase 1 HTTP contract for the QT control plane."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from qt.platform.commands import CommandRepository
from qt.platform.config import PlatformSettings
from qt.platform.database import (
    SessionFactory,
    create_platform_engine,
    create_session_factory,
)
from qt.platform.health import (
    Clock,
    ExpectedWorker,
    HealthService,
    ReadinessReport,
    WorkerHealthReport,
)
from qt.platform.models import utc_now
from qt.platform.operations import OperationsRepository
from qt.platform.schemas import CommandView


class LivenessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["alive"]


class ReadinessErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    detail: ReadinessReport


def create_app(
    *,
    settings: PlatformSettings | None = None,
    session_factory: SessionFactory | None = None,
    expected_workers: Sequence[ExpectedWorker] = (),
    clock: Clock = utc_now,
) -> FastAPI:
    """Construct one dependency graph and bind thin read-only route handlers."""

    resolved_settings = settings or PlatformSettings()
    resolved_session_factory = session_factory
    engine = None
    if resolved_session_factory is None:
        engine = create_platform_engine(resolved_settings)
        resolved_session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()

    commands = CommandRepository(resolved_session_factory, clock=clock)
    operations = OperationsRepository(resolved_session_factory, clock=clock)
    health = HealthService(
        resolved_session_factory,
        operations,
        expected_workers=expected_workers,
        stale_after_seconds=resolved_settings.worker_stale_seconds,
        clock=clock,
    )

    app = FastAPI(title="QT Control API", version="1.0.0", lifespan=lifespan)
    app.state.platform_engine = engine
    app.state.command_repository = commands
    app.state.health_service = health

    @app.get("/api/health/live", response_model=LivenessResponse)
    def liveness() -> LivenessResponse:
        return LivenessResponse(status="alive")

    @app.get(
        "/api/health/ready",
        response_model=ReadinessReport,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessErrorResponse}},
    )
    def readiness() -> ReadinessReport:
        report = health.readiness()
        if report.status == "not_ready":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=report.model_dump(mode="json"),
            )
        return report

    @app.get("/api/health/workers", response_model=WorkerHealthReport)
    def worker_health() -> WorkerHealthReport:
        return health.worker_health()

    @app.get("/api/v1/commands", response_model=list[CommandView])
    def list_commands(
        owner_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[CommandView]:
        return commands.list_recent(owner_id=owner_id, limit=limit)

    return app
