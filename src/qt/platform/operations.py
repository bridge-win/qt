"""Transactional worker heartbeat and append-only audit operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qt.platform.database import SessionFactory
from qt.platform.models import AuditEvent, WorkerHeartbeat, utc_now
from qt.platform.schemas import AuditEventView, WorkerHeartbeatView, WorkerStatus

Clock = Callable[[], datetime]


class OperationsRepository:
    """Persist worker health and audit history in repository-owned sessions."""

    def __init__(self, session_factory: SessionFactory, clock: Clock = utc_now) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def record_heartbeat(
        self,
        *,
        role: str,
        instance_id: str,
        status: WorkerStatus,
        version: str,
        details: Mapping[str, object],
    ) -> WorkerHeartbeatView:
        with self._session_factory() as session:
            while True:
                heartbeat = self._lock_heartbeat(session, role, instance_id)
                locked_at = self._now()
                if heartbeat is None:
                    heartbeat = WorkerHeartbeat(
                        role=role,
                        instance_id=instance_id,
                        status=status.value,
                        version=version,
                        details=dict(details),
                        last_seen_at=locked_at,
                        created_at=locked_at,
                        updated_at=locked_at,
                    )
                    session.add(heartbeat)
                    try:
                        session.commit()
                    except IntegrityError:
                        session.rollback()
                        continue
                    return _to_heartbeat_view(heartbeat)

                heartbeat.status = status.value
                heartbeat.version = version
                heartbeat.details = dict(details)
                heartbeat.last_seen_at = locked_at
                heartbeat.updated_at = locked_at
                session.commit()
                return _to_heartbeat_view(heartbeat)

    def list_heartbeats(self, *, role: str | None = None) -> list[WorkerHeartbeatView]:
        statement = select(WorkerHeartbeat)
        if role is not None:
            statement = statement.where(WorkerHeartbeat.role == role)
        statement = statement.order_by(WorkerHeartbeat.role, WorkerHeartbeat.instance_id)
        with self._session_factory() as session:
            return [_to_heartbeat_view(heartbeat) for heartbeat in session.scalars(statement)]

    def append_audit(
        self,
        *,
        actor_id: str,
        action: str,
        target_type: str,
        target_id: str,
        correlation_id: str,
        details: Mapping[str, object],
    ) -> AuditEventView:
        now = self._now()
        audit_event = AuditEvent(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            correlation_id=correlation_id,
            details=dict(details),
            created_at=now,
        )
        with self._session_factory() as session:
            session.add(audit_event)
            session.commit()
            return _to_audit_view(audit_event)

    def list_audit(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEventView]:
        if limit < 1:
            raise ValueError("limit must be positive")

        statement = select(AuditEvent)
        if target_type is not None:
            statement = statement.where(AuditEvent.target_type == target_type)
        if target_id is not None:
            statement = statement.where(AuditEvent.target_id == target_id)
        statement = statement.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(
            limit
        )
        with self._session_factory() as session:
            return [_to_audit_view(event) for event in session.scalars(statement)]

    @staticmethod
    def _lock_heartbeat(
        session: Session,
        role: str,
        instance_id: str,
    ) -> WorkerHeartbeat | None:
        return session.scalar(
            select(WorkerHeartbeat)
            .where(
                WorkerHeartbeat.role == role,
                WorkerHeartbeat.instance_id == instance_id,
            )
            .with_for_update()
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("operations repository clock must return an aware datetime")
        return now.astimezone(timezone.utc)


def _to_heartbeat_view(heartbeat: WorkerHeartbeat) -> WorkerHeartbeatView:
    return WorkerHeartbeatView.model_validate(heartbeat).model_copy(deep=True)


def _to_audit_view(audit_event: AuditEvent) -> AuditEventView:
    return AuditEventView.model_validate(audit_event).model_copy(deep=True)
