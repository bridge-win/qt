"""Database readiness and deterministic worker health evaluation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import text

from qt.platform.database import SessionFactory
from qt.platform.models import utc_now
from qt.platform.operations import OperationsRepository
from qt.platform.schemas import WorkerHeartbeatView, WorkerStatus

Clock = Callable[[], datetime]


class DependencyHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["healthy", "unavailable"]


class ReadinessDependencies(BaseModel):
    model_config = ConfigDict(frozen=True)

    database: DependencyHealth


class ReadinessReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "not_ready"]
    dependencies: ReadinessDependencies


class WorkerHealthStatus(str, Enum):
    HEALTHY = "healthy"
    STALE = "stale"
    MISSING = "missing"


class ExpectedWorker(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    instance_id: str


class WorkerHealthView(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    instance_id: str
    status: WorkerHealthStatus
    reported_status: WorkerStatus | None
    version: str | None
    last_seen_at: AwareDatetime | None


class WorkerHealthReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["healthy", "degraded"]
    workers: tuple[WorkerHealthView, ...]


class HealthService:
    """Evaluate control-plane dependencies without owning their lifecycle."""

    def __init__(
        self,
        session_factory: SessionFactory,
        operations: OperationsRepository,
        *,
        expected_workers: Sequence[ExpectedWorker] = (),
        stale_after_seconds: int,
        clock: Clock = utc_now,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self._session_factory = session_factory
        self._operations = operations
        self._expected_workers = tuple(
            sorted(
                {(worker.role, worker.instance_id): worker for worker in expected_workers}.values(),
                key=lambda worker: (worker.role, worker.instance_id),
            )
        )
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._clock = clock

    def readiness(self) -> ReadinessReport:
        try:
            with self._session_factory() as session:
                session.execute(text("SELECT 1")).scalar_one()
        except Exception:
            return ReadinessReport(
                status="not_ready",
                dependencies=ReadinessDependencies(
                    database=DependencyHealth(status="unavailable")
                ),
            )
        return ReadinessReport(
            status="ready",
            dependencies=ReadinessDependencies(database=DependencyHealth(status="healthy")),
        )

    def worker_health(self) -> WorkerHealthReport:
        now = self._now()
        heartbeats = {
            (heartbeat.role, heartbeat.instance_id): heartbeat
            for heartbeat in self._operations.list_heartbeats()
        }
        workers = tuple(
            self._evaluate_worker(expected, heartbeats.get((expected.role, expected.instance_id)), now)
            for expected in self._expected_workers
        )
        overall_status: Literal["healthy", "degraded"] = (
            "healthy"
            if all(worker.status is WorkerHealthStatus.HEALTHY for worker in workers)
            else "degraded"
        )
        return WorkerHealthReport(status=overall_status, workers=workers)

    def _evaluate_worker(
        self,
        expected: ExpectedWorker,
        heartbeat: WorkerHeartbeatView | None,
        now: datetime,
    ) -> WorkerHealthView:
        if heartbeat is None:
            return WorkerHealthView(
                role=expected.role,
                instance_id=expected.instance_id,
                status=WorkerHealthStatus.MISSING,
                reported_status=None,
                version=None,
                last_seen_at=None,
            )
        health_status = (
            WorkerHealthStatus.STALE
            if now - heartbeat.last_seen_at > self._stale_after
            else WorkerHealthStatus.HEALTHY
        )
        return WorkerHealthView(
            role=expected.role,
            instance_id=expected.instance_id,
            status=health_status,
            reported_status=heartbeat.status,
            version=heartbeat.version,
            last_seen_at=heartbeat.last_seen_at,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("health service clock must return an aware datetime")
        return now.astimezone(timezone.utc)
