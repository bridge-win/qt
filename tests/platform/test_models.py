from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, String, Table
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine
from qt.platform.models import (
    AuditEvent,
    Base,
    PlatformCommand,
    RuntimeLease,
    UTCDateTime,
    WorkerHeartbeat,
)
from qt.platform.schemas import (
    CommandStatus,
    CommandType,
    CommandView,
    LeaseGrant,
    WorkerStatus,
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    database_engine = create_platform_engine(
        PlatformSettings(
            platform_env="test",
            database_url="sqlite+pysqlite:///:memory:",
            database_echo=False,
            command_lease_seconds=30,
            worker_stale_seconds=60,
            _env_file=None,  # type: ignore[call-arg]
        )
    )
    Base.metadata.create_all(database_engine)
    yield database_engine
    Base.metadata.drop_all(database_engine)
    database_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as database_session:
        yield database_session


def make_command(
    *,
    owner_id: str = "operator",
    idempotency_key: str = "request-1",
    attempts: int = 0,
    max_attempts: int = 3,
) -> PlatformCommand:
    return PlatformCommand(
        owner_id=owner_id,
        command_type=CommandType.START.value,
        target="dca",
        payload={"mode": "paper"},
        idempotency_key=idempotency_key,
        attempts=attempts,
        max_attempts=max_attempts,
    )


def test_command_idempotency_is_unique_per_owner(session: Session) -> None:
    session.add(make_command(idempotency_key="request-1"))
    session.commit()
    session.add(make_command(idempotency_key="request-1"))

    with pytest.raises(IntegrityError):
        session.commit()


def test_command_attempt_counts_and_statuses_are_constrained(session: Session) -> None:
    session.add(make_command(attempts=-1))
    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()
    invalid_status = make_command(idempotency_key="request-2")
    invalid_status.status = "unknown"
    session.add(invalid_status)
    with pytest.raises(IntegrityError):
        session.commit()


def test_command_uses_bounded_strings_and_claimable_index() -> None:
    command_table = cast(Table, PlatformCommand.__table__)
    indexes: dict[str, tuple[str, ...]] = {
        str(index.name): tuple(column.name for column in index.columns)
        for index in command_table.indexes
    }

    assert isinstance(PlatformCommand.__table__.c.command_type.type, String)
    assert isinstance(PlatformCommand.__table__.c.status.type, String)
    assert indexes["ix_command_claimable"] == ("status", "available_at", "created_at")


def test_runtime_lease_identity_and_fencing_token_are_constrained(session: Session) -> None:
    session.add(
        RuntimeLease(
            resource_type="strategy",
            resource_id="dca",
            owner_id="worker-a",
            fencing_token=1,
        )
    )
    session.commit()
    session.add(
        RuntimeLease(
            resource_type="strategy",
            resource_id="dca",
            owner_id="worker-b",
            fencing_token=2,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()
    session.add(
        RuntimeLease(
            resource_type="strategy",
            resource_id="other",
            owner_id="worker-a",
            fencing_token=0,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_worker_heartbeat_identity_is_unique(session: Session) -> None:
    session.add(
        WorkerHeartbeat(
            role="trading",
            instance_id="worker-a",
            status=WorkerStatus.HEALTHY.value,
            version="1.0.0",
            details={},
        )
    )
    session.commit()
    session.add(
        WorkerHeartbeat(
            role="trading",
            instance_id="worker-a",
            status=WorkerStatus.DEGRADED.value,
            version="1.0.0",
            details={},
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_audit_event_has_no_update_timestamp() -> None:
    columns = AuditEvent.__table__.columns

    assert "created_at" in columns
    assert "updated_at" not in columns


def test_datetime_columns_use_utc_datetime_type() -> None:
    columns = (
        (PlatformCommand, "available_at"),
        (PlatformCommand, "claim_expires_at"),
        (PlatformCommand, "created_at"),
        (PlatformCommand, "updated_at"),
        (PlatformCommand, "completed_at"),
        (RuntimeLease, "expires_at"),
        (RuntimeLease, "created_at"),
        (RuntimeLease, "updated_at"),
        (WorkerHeartbeat, "last_seen_at"),
        (WorkerHeartbeat, "created_at"),
        (WorkerHeartbeat, "updated_at"),
        (AuditEvent, "created_at"),
    )

    assert all(
        isinstance(model.__table__.c[column_name].type, UTCDateTime)
        for model, column_name in columns
    )


def test_datetime_columns_reject_naive_values(session: Session) -> None:
    command = make_command()
    command.available_at = datetime(2026, 7, 21, 0, 0)
    session.add(command)

    with pytest.raises(StatementError, match="aware UTC datetime"):
        session.commit()


def test_datetime_values_reload_as_aware_utc_and_build_command_view(session: Session) -> None:
    offset_datetime = datetime(2026, 7, 21, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    command = make_command()
    command.claim_expires_at = offset_datetime
    command.completed_at = offset_datetime
    lease = RuntimeLease(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        fencing_token=1,
        expires_at=offset_datetime,
    )
    heartbeat = WorkerHeartbeat(
        role="trading",
        instance_id="worker-a",
        status=WorkerStatus.HEALTHY.value,
        version="1.0.0",
        details={},
        last_seen_at=offset_datetime,
    )
    audit_event = AuditEvent(
        actor_id="operator",
        action="strategy.start",
        target_type="strategy",
        target_id="dca",
        correlation_id="request-1",
        details={},
        created_at=offset_datetime,
    )
    session.add_all([command, lease, heartbeat, audit_event])
    session.commit()
    command_id = command.id
    lease_id = lease.id
    heartbeat_id = heartbeat.id
    audit_event_id = audit_event.id
    session.expunge_all()

    reloaded_command = session.get(PlatformCommand, command_id)
    reloaded_lease = session.get(RuntimeLease, lease_id)
    reloaded_heartbeat = session.get(WorkerHeartbeat, heartbeat_id)
    reloaded_audit_event = session.get(AuditEvent, audit_event_id)

    assert reloaded_command is not None
    assert reloaded_lease is not None
    assert reloaded_heartbeat is not None
    assert reloaded_audit_event is not None
    timestamps = (
        reloaded_command.available_at,
        reloaded_command.claim_expires_at,
        reloaded_command.created_at,
        reloaded_command.updated_at,
        reloaded_command.completed_at,
        reloaded_lease.expires_at,
        reloaded_lease.created_at,
        reloaded_lease.updated_at,
        reloaded_heartbeat.last_seen_at,
        reloaded_heartbeat.created_at,
        reloaded_heartbeat.updated_at,
        reloaded_audit_event.created_at,
    )

    assert all(timestamp is not None and timestamp.tzinfo is timezone.utc for timestamp in timestamps)
    assert CommandView.model_validate(reloaded_command).completed_at is not None


def test_audit_event_rejects_orm_updates_and_deletes(session: Session) -> None:
    audit_event = AuditEvent(
        actor_id="operator",
        action="strategy.start",
        target_type="strategy",
        target_id="dca",
        correlation_id="request-1",
        details={},
    )
    session.add(audit_event)
    session.commit()

    audit_event.action = "strategy.stop"
    with pytest.raises(ValueError, match="append-only"):
        session.flush()

    session.rollback()
    session.delete(audit_event)
    with pytest.raises(ValueError, match="append-only"):
        session.flush()


def test_utc_defaults_are_aware(session: Session) -> None:
    command = make_command()
    lease = RuntimeLease(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        fencing_token=1,
    )
    heartbeat = WorkerHeartbeat(
        role="trading",
        instance_id="worker-a",
        status=WorkerStatus.HEALTHY.value,
        version="1.0.0",
        details={},
    )
    audit_event = AuditEvent(
        actor_id="operator",
        action="strategy.start",
        target_type="strategy",
        target_id="dca",
        correlation_id="request-1",
        details={},
    )
    session.add_all([command, lease, heartbeat, audit_event])
    session.flush()

    assert command.available_at.tzinfo is timezone.utc
    assert command.created_at.tzinfo is timezone.utc
    assert command.updated_at.tzinfo is timezone.utc
    assert lease.expires_at.tzinfo is timezone.utc
    assert heartbeat.last_seen_at.tzinfo is timezone.utc
    assert audit_event.created_at.tzinfo is timezone.utc


def test_service_views_are_frozen_and_require_aware_datetimes(session: Session) -> None:
    command = make_command()
    session.add(command)
    session.flush()

    view = CommandView.model_validate(command)

    assert view.status is CommandStatus.PENDING
    assert view.command_type is CommandType.START
    with pytest.raises(ValidationError):
        view.status = CommandStatus.CANCELLED

    with pytest.raises(ValidationError):
        LeaseGrant.model_validate(
            {
                "resource_type": "strategy",
                "resource_id": "dca",
                "owner_id": "worker-a",
                "fencing_token": 1,
                "expires_at": "2026-07-21T00:00:00",
            }
        )
