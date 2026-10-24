"""Transactional registry, fenced jobs and append-only business events."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from typing import Any

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from qt.contracts import canonical_json
from qt.persistence.schema import artifacts, events, jobs, runtime_health


class ConflictError(ValueError):
    """A stable identity was reused for different immutable content."""


Conflict = ConflictError


def canonical(value: Any) -> str:
    return canonical_json(value)


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def insert_once(connection: Connection, table, values: dict) -> None:
    constructor = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
    connection.execute(constructor(table).values(**values).on_conflict_do_nothing())


class Repository:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self):
        # PostgreSQL row locks coordinate processes. The local lock additionally
        # serializes SQLite's single-connection offline tests.
        with self._lock, self.engine.begin() as connection:
            yield connection

    @staticmethod
    def event(connection: Connection, tenant: str, entity: str, kind: str, payload: dict) -> None:
        connection.execute(insert(events).values(
            tenant_id=tenant, entity_id=entity, event_type=kind,
            payload=payload, created_ns=time.time_ns(),
        ))

    @staticmethod
    def assert_job_fence(connection: Connection, tenant: str, fence: tuple[str, str]) -> None:
        active = connection.execute(select(jobs).where(
            jobs.c.tenant_id == tenant, jobs.c.job_id == fence[0],
        ).with_for_update()).mappings().first()
        if (active is None or active["lease_token"] != fence[1]
                or active["status"] != "RUNNING" or active["cancel_requested"]
                or active["lease_until_ns"] <= time.time_ns()
                or active["deadline_ns"] <= time.time_ns()):
            raise Conflict("job_lease_lost")

    def put(self, tenant: str, kind: str, payload: dict, entity_id: str | None = None,
            *, fence_job: tuple[str, str] | None = None,
            connection: Connection | None = None) -> dict:
        digest = checksum(payload)
        identity = entity_id or digest
        with nullcontext(connection) if connection is not None else self.transaction() as connection:
            if fence_job is not None:
                self.assert_job_fence(connection, tenant, fence_job)
            insert_once(connection, artifacts, {
                "tenant_id": tenant, "kind": kind, "entity_id": identity,
                "payload": payload, "sha256": digest, "created_ns": time.time_ns(),
            })
            row = connection.execute(select(artifacts).where(
                artifacts.c.tenant_id == tenant, artifacts.c.kind == kind,
                artifacts.c.entity_id == identity,
            )).mappings().one()
            if row["sha256"] != digest:
                raise Conflict("immutable_artifact_identity_conflict")
        return dict(row)

    def get(self, tenant: str, kind: str, identity: str) -> dict:
        with self.transaction() as connection:
            row = connection.execute(select(artifacts).where(
                artifacts.c.tenant_id == tenant, artifacts.c.kind == kind,
                artifacts.c.entity_id == identity,
            )).mappings().first()
        if row is None:
            raise KeyError("artifact_not_found")
        if checksum(row["payload"]) != row["sha256"]:
            raise Conflict("artifact_integrity_failure")
        return dict(row)

    def list(self, tenant: str, kind: str, *, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 200:
            raise ValueError("limit_out_of_range")
        with self.transaction() as connection:
            rows = connection.execute(select(artifacts).where(
                artifacts.c.tenant_id == tenant, artifacts.c.kind == kind,
            ).order_by(artifacts.c.created_ns.desc(), artifacts.c.entity_id).limit(limit)).mappings()
            return [dict(row) for row in rows]

    def enqueue(self, tenant: str, kind: str, payload: dict, key: str,
                *, deadline_ns: int | None = None, fence_job: tuple[str, str] | None = None,
                connection: Connection | None = None) -> dict:
        if not key or len(key) > 200:
            raise ValueError("invalid_idempotency_key")
        now = time.time_ns()
        identity = checksum([tenant, kind, key])
        digest = checksum(payload)
        with nullcontext(connection) if connection is not None else self.transaction() as connection:
            if fence_job is not None:
                self.assert_job_fence(connection, tenant, fence_job)
            insert_once(connection, jobs, {
                "job_id": identity, "tenant_id": tenant, "kind": kind,
                "idempotency_key": key, "payload": payload, "sha256": digest,
                "status": "QUEUED", "created_ns": now,
                "deadline_ns": deadline_ns or now + 900_000_000_000,
                "lease_until_ns": 0, "attempts": 0, "cancel_requested": 0,
            })
            row = connection.execute(select(jobs).where(jobs.c.job_id == identity)).mappings().one()
            if row["sha256"] != digest:
                raise Conflict("idempotency_key_payload_conflict")
        return dict(row)

    def job(self, tenant: str, job_id: str) -> dict:
        with self.transaction() as connection:
            row = connection.execute(select(jobs).where(
                jobs.c.tenant_id == tenant, jobs.c.job_id == job_id,
            )).mappings().first()
        if row is None:
            raise KeyError("job_not_found")
        return dict(row)

    def claim(self, kind: str, *, lease_seconds: int = 60, now_ns: int | None = None,
              tenant: str | None = None) -> dict | None:
        with self.transaction() as connection:
            now = now_ns if now_ns is not None else time.time_ns()
            expired = connection.execute(select(jobs).where(
                jobs.c.kind == kind, jobs.c.status.in_(["QUEUED", "RUNNING"]),
                jobs.c.tenant_id == tenant if tenant is not None else True,
                jobs.c.lease_until_ns <= now,
                or_(jobs.c.deadline_ns <= now, jobs.c.attempts >= 3,
                    jobs.c.cancel_requested == 1),
            ).with_for_update(skip_locked=True)).mappings().all()
            for row in expired:
                state = "CANCELLED" if row["cancel_requested"] else "EXHAUSTED"
                connection.execute(update(jobs).where(jobs.c.job_id == row["job_id"]).values(status=state))
                self.event(connection, row["tenant_id"], row["job_id"], state, {})
            row = connection.execute(select(jobs).where(
                jobs.c.kind == kind, jobs.c.status.in_(["QUEUED", "RUNNING"]),
                jobs.c.tenant_id == tenant if tenant is not None else True,
                jobs.c.lease_until_ns <= now, jobs.c.deadline_ns > now,
                jobs.c.attempts < 3, jobs.c.cancel_requested == 0,
            ).order_by(jobs.c.created_ns, jobs.c.job_id)
                .with_for_update(skip_locked=True).limit(1)).mappings().first()
            if row is None:
                return None
            token = uuid.uuid4().hex
            values = {"status": "RUNNING", "lease_token": token,
                      "lease_until_ns": now + lease_seconds * 1_000_000_000,
                      "attempts": row["attempts"] + 1}
            connection.execute(update(jobs).where(jobs.c.job_id == row["job_id"]).values(**values))
            self.event(connection, row["tenant_id"], row["job_id"], "claimed",
                       {"attempt": values["attempts"]})
            return {**dict(row), **values}

    def heartbeat(self, job: dict, *, lease_seconds: int = 60) -> bool:
        with self.transaction() as connection:
            row = connection.execute(select(jobs).where(
                jobs.c.job_id == job["job_id"], jobs.c.tenant_id == job["tenant_id"],
            ).with_for_update()).mappings().first()
            # Sampling before a row-lock wait could resurrect a lease which
            # expired while this transaction was blocked behind another writer.
            now = time.time_ns()
            if (row is None or row["lease_token"] != job["lease_token"]
                    or row["status"] != "RUNNING" or row["cancel_requested"]
                    or row["lease_until_ns"] <= now or row["deadline_ns"] <= now):
                return False
            connection.execute(update(jobs).where(jobs.c.job_id == job["job_id"]).values(
                lease_until_ns=now + lease_seconds * 1_000_000_000))
            return True

    def finish(self, job: dict, result: dict, *, failed: bool = False) -> dict:
        canonical(result)
        with self.transaction() as connection:
            row = connection.execute(select(jobs).where(
                jobs.c.job_id == job["job_id"], jobs.c.tenant_id == job["tenant_id"],
            ).with_for_update()).mappings().one()
            if (row["lease_token"] != job["lease_token"] or row["status"] != "RUNNING"
                    or row["lease_until_ns"] <= time.time_ns()
                    or row["deadline_ns"] <= time.time_ns()):
                raise Conflict("job_lease_lost")
            status = "CANCELLED" if row["cancel_requested"] else ("FAILED" if failed else "COMPLETED")
            connection.execute(update(jobs).where(jobs.c.job_id == job["job_id"]).values(
                status=status, result=result, lease_until_ns=0,
            ))
            self.event(connection, job["tenant_id"], job["job_id"], status, result)
        return self.job(job["tenant_id"], job["job_id"])

    def cancel(self, tenant: str, job_id: str) -> dict:
        with self.transaction() as connection:
            row = connection.execute(select(jobs).where(
                jobs.c.tenant_id == tenant, jobs.c.job_id == job_id,
            ).with_for_update()).mappings().first()
            if row is None:
                raise KeyError("job_not_found")
            if row["status"] in {"QUEUED", "RUNNING"}:
                connection.execute(update(jobs).where(jobs.c.job_id == job_id).values(
                    cancel_requested=1,
                    status="CANCELLED" if row["status"] == "QUEUED" else "RUNNING",
                ))
                self.event(connection, tenant, job_id, "cancel_requested", {})
        return self.job(tenant, job_id)

    def event_page(self, tenant: str, entity: str, *, after: int = 0, limit: int = 100) -> list[dict]:
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError("invalid_event_cursor_or_limit")
        with self.transaction() as connection:
            rows = connection.execute(select(events).where(
                events.c.tenant_id == tenant, events.c.entity_id == entity,
                events.c.sequence > after,
            ).order_by(events.c.sequence).limit(limit)).mappings()
            return [dict(row) for row in rows]

    def health(self, tenant: str, name: str, payload: dict) -> None:
        with self.transaction() as connection:
            insert_once(connection, runtime_health, {
                "tenant_id": tenant, "name": name,
                "observed_ns": time.time_ns(), "payload": payload,
            })
            connection.execute(update(runtime_health).where(and_(
                runtime_health.c.tenant_id == tenant, runtime_health.c.name == name,
            )).values(observed_ns=time.time_ns(), payload=payload))
