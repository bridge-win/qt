"""Transactional runtime lease ownership with monotonic fencing."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qt.platform.database import SessionFactory
from qt.platform.models import RuntimeLease, utc_now
from qt.platform.schemas import LeaseGrant

Clock = Callable[[], datetime]


class LeaseRepository:
    """Acquire and transition runtime leases in repository-owned sessions."""

    def __init__(self, session_factory: SessionFactory, clock: Clock = utc_now) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def acquire(
        self,
        *,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        ttl_seconds: int,
    ) -> LeaseGrant | None:
        self._require_positive_ttl(ttl_seconds)

        with self._session_factory() as session:
            while True:
                lease = self._lock_resource(session, resource_type, resource_id)
                locked_at = self._now()
                expires_at = locked_at + timedelta(seconds=ttl_seconds)
                if lease is None:
                    lease = RuntimeLease(
                        resource_type=resource_type,
                        resource_id=resource_id,
                        owner_id=owner_id,
                        fencing_token=1,
                        expires_at=expires_at,
                        created_at=locked_at,
                        updated_at=locked_at,
                    )
                    session.add(lease)
                    try:
                        session.commit()
                    except IntegrityError:
                        session.rollback()
                        continue
                    return _to_grant(lease)

                if lease.expires_at > locked_at:
                    if lease.owner_id != owner_id:
                        return None
                else:
                    lease.owner_id = owner_id
                    lease.fencing_token += 1

                lease.expires_at = expires_at
                lease.version += 1
                lease.updated_at = locked_at
                session.commit()
                return _to_grant(lease)

    def renew(
        self,
        *,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        fencing_token: int,
        ttl_seconds: int,
    ) -> LeaseGrant | None:
        self._require_positive_ttl(ttl_seconds)

        with self._session_factory() as session:
            lease = self._lock_resource(session, resource_type, resource_id)
            locked_at = self._now()
            lease = self._active_owner(lease, owner_id, fencing_token, locked_at)
            if lease is None:
                return None

            lease.expires_at = locked_at + timedelta(seconds=ttl_seconds)
            lease.version += 1
            lease.updated_at = locked_at
            session.commit()
            return _to_grant(lease)

    def release(
        self,
        *,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> bool:
        with self._session_factory() as session:
            lease = self._lock_resource(session, resource_type, resource_id)
            locked_at = self._now()
            lease = self._active_owner(lease, owner_id, fencing_token, locked_at)
            if lease is None:
                return False

            lease.expires_at = locked_at
            lease.version += 1
            lease.updated_at = locked_at
            session.commit()
            return True

    @staticmethod
    def _lock_resource(
        session: Session,
        resource_type: str,
        resource_id: str,
    ) -> RuntimeLease | None:
        return session.scalar(
            select(RuntimeLease)
            .where(
                RuntimeLease.resource_type == resource_type,
                RuntimeLease.resource_id == resource_id,
            )
            .with_for_update()
        )

    @staticmethod
    def _active_owner(
        lease: RuntimeLease | None,
        owner_id: str,
        fencing_token: int,
        now: datetime,
    ) -> RuntimeLease | None:
        if (
            lease is not None
            and lease.owner_id == owner_id
            and lease.fencing_token == fencing_token
            and lease.expires_at > now
        ):
            return lease
        return None

    @staticmethod
    def _require_positive_ttl(ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("lease repository clock must return an aware datetime")
        return now.astimezone(timezone.utc)


def _to_grant(lease: RuntimeLease) -> LeaseGrant:
    return LeaseGrant(
        resource_type=lease.resource_type,
        resource_id=lease.resource_id,
        owner_id=lease.owner_id,
        fencing_token=lease.fencing_token,
        expires_at=lease.expires_at,
    )
