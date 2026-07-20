"""Transactional repository for durable platform commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qt.platform.database import SessionFactory
from qt.platform.models import PlatformCommand, utc_now
from qt.platform.schemas import CommandStatus, CommandType, CommandView

Clock = Callable[[], datetime]


class StaleCommandClaimError(RuntimeError):
    """Raised when a worker no longer owns an active command claim."""


class CommandRepository:
    """Persist and transition durable commands in repository-owned sessions."""

    def __init__(self, session_factory: SessionFactory, clock: Clock = utc_now) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def enqueue(
        self,
        *,
        owner_id: str,
        command_type: CommandType,
        target: str,
        payload: Mapping[str, object],
        idempotency_key: str,
        max_attempts: int = 3,
    ) -> CommandView:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        now = self._now()
        with self._session_factory() as session:
            existing = self._find_idempotent(session, owner_id, idempotency_key)
            if existing is not None:
                return _to_view(existing)

            command = PlatformCommand(
                owner_id=owner_id,
                command_type=command_type.value,
                target=target,
                payload=dict(payload),
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(command)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self._find_idempotent(session, owner_id, idempotency_key)
                if existing is None:
                    raise
                return _to_view(existing)
            return _to_view(command)

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> CommandView | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        now = self._now()
        claimable = or_(
            and_(
                PlatformCommand.status.in_(
                    (CommandStatus.PENDING.value, CommandStatus.RETRY_WAIT.value)
                ),
                PlatformCommand.available_at <= now,
            ),
            and_(
                PlatformCommand.status == CommandStatus.PROCESSING.value,
                PlatformCommand.claim_expires_at <= now,
            ),
        )
        statement = (
            select(PlatformCommand)
            .where(
                claimable,
                PlatformCommand.attempts < PlatformCommand.max_attempts,
            )
            .order_by(PlatformCommand.available_at, PlatformCommand.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        with self._session_factory() as session:
            command = session.scalar(statement)
            if command is None:
                return None

            command.status = CommandStatus.PROCESSING.value
            command.attempts += 1
            command.claim_owner = worker_id
            command.claim_token = uuid4()
            command.claim_expires_at = now + timedelta(seconds=lease_seconds)
            command.result = None
            command.error = None
            command.completed_at = None
            command.version += 1
            command.updated_at = now
            session.commit()
            return _to_view(command)

    def complete(
        self,
        *,
        command_id: UUID,
        claim_token: UUID,
        result: Mapping[str, object],
    ) -> CommandView:
        now = self._now()
        with self._session_factory() as session:
            command = self._lock_owned_claim(session, command_id, claim_token, now)
            command.status = CommandStatus.SUCCEEDED.value
            command.result = dict(result)
            command.error = None
            command.completed_at = now
            self._clear_claim(command)
            command.version += 1
            command.updated_at = now
            session.commit()
            return _to_view(command)

    def fail(
        self,
        *,
        command_id: UUID,
        claim_token: UUID,
        error: str,
        retry_delay_seconds: int | None,
    ) -> CommandView:
        if retry_delay_seconds is not None and retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds cannot be negative")

        now = self._now()
        with self._session_factory() as session:
            command = self._lock_owned_claim(session, command_id, claim_token, now)
            command.result = None
            command.error = error
            self._clear_claim(command)
            if retry_delay_seconds is not None and command.attempts < command.max_attempts:
                command.status = CommandStatus.RETRY_WAIT.value
                command.available_at = now + timedelta(seconds=retry_delay_seconds)
                command.completed_at = None
            else:
                command.status = CommandStatus.FAILED.value
                command.completed_at = now
            command.version += 1
            command.updated_at = now
            session.commit()
            return _to_view(command)

    def get(self, command_id: UUID) -> CommandView | None:
        with self._session_factory() as session:
            command = session.get(PlatformCommand, command_id)
            return None if command is None else _to_view(command)

    def list_recent(
        self,
        *,
        owner_id: str | None = None,
        limit: int = 50,
    ) -> list[CommandView]:
        if limit < 1:
            raise ValueError("limit must be positive")

        statement = select(PlatformCommand)
        if owner_id is not None:
            statement = statement.where(PlatformCommand.owner_id == owner_id)
        statement = statement.order_by(
            PlatformCommand.created_at.desc(),
            PlatformCommand.id.desc(),
        ).limit(limit)
        with self._session_factory() as session:
            return [_to_view(command) for command in session.scalars(statement)]

    def _find_idempotent(
        self,
        session: Session,
        owner_id: str,
        idempotency_key: str,
    ) -> PlatformCommand | None:
        return session.scalar(
            select(PlatformCommand).where(
                PlatformCommand.owner_id == owner_id,
                PlatformCommand.idempotency_key == idempotency_key,
            )
        )

    def _lock_owned_claim(
        self,
        session: Session,
        command_id: UUID,
        claim_token: UUID,
        now: datetime,
    ) -> PlatformCommand:
        command = session.scalar(
            select(PlatformCommand)
            .where(PlatformCommand.id == command_id)
            .with_for_update()
        )
        if (
            command is None
            or command.status != CommandStatus.PROCESSING.value
            or command.claim_token != claim_token
            or command.claim_expires_at is None
            or command.claim_expires_at <= now
        ):
            raise StaleCommandClaimError(f"command {command_id} has no matching active claim")
        return command

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("command repository clock must return an aware datetime")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _clear_claim(command: PlatformCommand) -> None:
        command.claim_owner = None
        command.claim_token = None
        command.claim_expires_at = None


def _to_view(command: PlatformCommand) -> CommandView:
    return CommandView.model_validate(command).model_copy(deep=True)
