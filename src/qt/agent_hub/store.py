"""Tenant-scoped SQLite WAL jobs, schedules, leases and append-only events.

The ledger fences state transitions, not external effects. Executors must also
use the job ID as an idempotency key when invoking a side-effecting service.
Schedules coalesce missed intervals into one current-bucket job; they do not
replay an unbounded backlog after downtime. Tenant IDs are trusted application
context supplied by the caller, not authentication credentials.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

JsonDict = dict[str, object]
_STATUSES = ("queued", "running", "succeeded", "deadletter")
_MAX_TIMESTAMP = 253_402_300_799.0
_MAX_JSON_BYTES = 1_048_576


class HubStore:
    """Single-file durable ledger; separate instances/processes share WAL locks."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._open()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS hub_jobs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    initial_not_before REAL,
                    not_before REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued', 'running', 'succeeded', 'deadletter')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 100),
                    worker_id TEXT,
                    lease_token INTEGER NOT NULL DEFAULT 0 CHECK(lease_token >= 0),
                    lease_until REAL,
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL,
                    UNIQUE(tenant_id, id),
                    UNIQUE(tenant_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS hub_jobs_claim
                    ON hub_jobs(tenant_id, status, not_before, created_at);
                CREATE INDEX IF NOT EXISTS hub_jobs_expiry
                    ON hub_jobs(tenant_id, status, lease_until);
                CREATE INDEX IF NOT EXISTS hub_jobs_history
                    ON hub_jobs(tenant_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS hub_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    job_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(tenant_id, job_id) REFERENCES hub_jobs(tenant_id, id)
                );
                CREATE INDEX IF NOT EXISTS hub_events_tenant
                    ON hub_events(tenant_id, id);
                CREATE INDEX IF NOT EXISTS hub_events_job
                    ON hub_events(tenant_id, job_id, id);
                CREATE TABLE IF NOT EXISTS hub_schedules (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    interval_seconds REAL NOT NULL CHECK(interval_seconds >= 1),
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    next_run_at REAL NOT NULL,
                    last_run_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(tenant_id, name)
                );
                CREATE INDEX IF NOT EXISTS hub_schedules_due
                    ON hub_schedules(tenant_id, enabled, next_run_at);
                COMMIT;
                """
            )
        finally:
            connection.close()

    def enqueue(
        self,
        tenant_id: str,
        kind: str,
        payload: JsonDict,
        *,
        idempotency_key: str,
        not_before: float | None = None,
        max_attempts: int = 3,
    ) -> JsonDict:
        tenant_id = _text(tenant_id, "tenant_id")
        kind = _text(kind, "kind")
        key = _text(idempotency_key, "idempotency_key", maximum=256)
        if key.startswith("schedule:"):
            raise ValueError("idempotency_key prefix 'schedule:' is reserved")
        encoded = _json(payload)
        initial = (
            _number(not_before, "not_before", 0, _MAX_TIMESTAMP) if not_before is not None else None
        )
        attempts = _integer(max_attempts, "max_attempts", 1, 100)
        with self._connection(write=True) as connection:
            return self._enqueue(
                connection, tenant_id, kind, encoded, key, initial, attempts, _now()
            )

    def claim(
        self,
        tenant_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 180,
        kinds: list[str] | None = None,
    ) -> JsonDict | None:
        tenant_id = _text(tenant_id, "tenant_id")
        worker_id = _text(worker_id, "worker_id")
        lease = _number(lease_seconds, "lease_seconds", 1, 86_400)
        if kinds is not None:
            if not isinstance(kinds, list) or len(kinds) > 100:
                raise ValueError("kinds must be a list of at most 100 kinds")
            kinds = sorted({_text(kind, "kind") for kind in kinds})
        with self._connection(write=True) as connection:
            now = _now()
            self._expire(connection, tenant_id, now)
            if kinds == []:
                return None
            filters = ""
            params: list[object] = [tenant_id, now]
            if kinds is not None:
                filters = " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
                params.extend(kinds)
            row = connection.execute(
                "SELECT * FROM hub_jobs WHERE tenant_id=? AND status='queued' "
                "AND not_before<=? AND attempts<max_attempts"
                + filters
                + " ORDER BY not_before, created_at, rowid LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE hub_jobs SET status='running', attempts=attempts+1, "
                "worker_id=?, lease_token=lease_token+1, lease_until=?, updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (worker_id, now + lease, now, tenant_id, row["id"]),
            )
            job = self._get(connection, tenant_id, row["id"])
            self._append_event(
                connection,
                tenant_id,
                row["id"],
                "job.claimed",
                {
                    "worker_id": worker_id,
                    "lease_token": job["lease_token"],
                    "attempts": job["attempts"],
                },
                now,
            )
            return job

    def heartbeat(
        self,
        tenant_id: str,
        job_id: str,
        worker_id: str,
        lease_token: int,
        *,
        lease_seconds: float = 180,
    ) -> bool:
        identity = _identity(tenant_id, job_id, worker_id, lease_token)
        lease = _number(lease_seconds, "lease_seconds", 1, 86_400)
        with self._connection(write=True) as connection:
            now = _now()
            return (
                connection.execute(
                    "UPDATE hub_jobs SET lease_until=MAX(lease_until, ?), updated_at=? "
                    "WHERE tenant_id=? AND id=? AND worker_id=? AND lease_token=? "
                    "AND status='running' AND lease_until>?",
                    (now + lease, now, *identity, now),
                ).rowcount
                == 1
            )

    def complete(
        self,
        tenant_id: str,
        job_id: str,
        worker_id: str,
        lease_token: int,
        result: JsonDict,
    ) -> bool:
        return self.complete_and_enqueue(tenant_id, job_id, worker_id, lease_token, result)

    def complete_and_enqueue(
        self,
        tenant_id: str,
        job_id: str,
        worker_id: str,
        lease_token: int,
        result: JsonDict,
        *,
        next_kind: str | None = None,
        next_payload: JsonDict | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        """Commit this result and its next durable step in one fenced transaction.

        A retry after a committed response was lost returns False and cannot
        duplicate the follow-up. An enqueue conflict rolls back the completion.
        """
        identity = _identity(tenant_id, job_id, worker_id, lease_token)
        encoded = _json(result)
        next_encoded: str | None = None
        if next_kind is None:
            if next_payload is not None or idempotency_key is not None:
                raise ValueError("next_payload/idempotency_key require next_kind")
        else:
            next_kind = _text(next_kind, "next_kind")
            if next_payload is None or idempotency_key is None:
                raise ValueError("next_kind requires next_payload and idempotency_key")
            next_encoded = _json(next_payload)
            idempotency_key = _text(idempotency_key, "idempotency_key", maximum=256)
            if idempotency_key.startswith("schedule:"):
                raise ValueError("idempotency_key prefix 'schedule:' is reserved")
        with self._connection(write=True) as connection:
            now = _now()
            changed = (
                connection.execute(
                    "UPDATE hub_jobs SET status='succeeded', result_json=?, error=NULL, "
                    "lease_until=NULL, updated_at=?, finished_at=? "
                    "WHERE tenant_id=? AND id=? AND worker_id=? AND lease_token=? "
                    "AND status='running' AND lease_until>?",
                    (encoded, now, now, *identity, now),
                ).rowcount
                == 1
            )
            if changed:
                self._append_event(
                    connection,
                    identity[0],
                    identity[1],
                    "job.succeeded",
                    {
                        "worker_id": identity[2],
                        "lease_token": identity[3],
                    },
                    now,
                )
                if (
                    next_kind is not None
                    and next_encoded is not None
                    and idempotency_key is not None
                ):
                    followup = self._enqueue(
                        connection,
                        identity[0],
                        next_kind,
                        next_encoded,
                        idempotency_key,
                        None,
                        3,
                        now,
                    )
                    if followup["id"] == identity[1]:
                        raise ValueError(
                            "follow-up idempotency key must not reference the current job"
                        )
                    self._append_event(
                        connection,
                        identity[0],
                        identity[1],
                        "job.followup_enqueued",
                        {
                            "next_job_id": followup["id"],
                            "next_kind": next_kind,
                        },
                        now,
                    )
            return changed

    def requeue_deadletter(self, tenant_id: str, job_id: str) -> JsonDict:
        """Operator recovery starts a fresh attempt budget, retaining fencing history."""
        tenant_id, job_id = _text(tenant_id, "tenant_id"), _text(job_id, "job_id")
        with self._connection(write=True) as connection:
            previous = self._get(connection, tenant_id, job_id)
            if previous["status"] != "deadletter":
                raise ValueError("only deadletter jobs can be requeued")
            now = _now()
            connection.execute(
                "UPDATE hub_jobs SET status='queued',attempts=0,worker_id=NULL,lease_until=NULL,"
                "result_json=NULL,error=NULL,finished_at=NULL,not_before=?,updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (now, now, tenant_id, job_id),
            )
            self._append_event(
                connection,
                tenant_id,
                job_id,
                "job.requeued",
                {
                    "previous_attempts": previous["attempts"],
                    "previous_error": previous["error"],
                    "lease_token": previous["lease_token"],
                },
                now,
            )
            return self._get(connection, tenant_id, job_id)

    def defer(
        self,
        tenant_id: str,
        job_id: str,
        worker_id: str,
        lease_token: int,
        *,
        retry_delay: float = 5,
        reason: str = "executor_busy",
    ) -> bool:
        """Release a live lease after admission rejection, preserving failure budget.

        Only the fenced owner may undo its own claim's attempt increment. The
        fencing token is never decremented. Small jitter avoids synchronized
        retries by workers sharing the same serialized model executor.
        """
        identity = _identity(tenant_id, job_id, worker_id, lease_token)
        delay = _number(retry_delay, "retry_delay", 1, 604_800)
        reason = _text(reason, "reason")
        delay += random.uniform(0, min(5.0, delay * 0.2))
        with self._connection(write=True) as connection:
            now = _now()
            changed = (
                connection.execute(
                    "UPDATE hub_jobs SET status='queued',attempts=attempts-1,worker_id=NULL,"
                    "lease_until=NULL,not_before=?,updated_at=? WHERE tenant_id=? AND id=? "
                    "AND worker_id=? AND lease_token=? AND status='running' AND lease_until>? "
                    "AND attempts>0",
                    (now + delay, now, *identity, now),
                ).rowcount
                == 1
            )
            if changed:
                self._append_event(
                    connection,
                    identity[0],
                    identity[1],
                    "job.deferred",
                    {"reason": reason, "retry_delay": delay, "lease_token": identity[3]},
                    now,
                )
            return changed

    def fail(
        self,
        tenant_id: str,
        job_id: str,
        worker_id: str,
        lease_token: int,
        error: str,
        *,
        retry_delay: float = 5,
    ) -> bool:
        identity = _identity(tenant_id, job_id, worker_id, lease_token)
        error = _text(error, "error", maximum=8192)
        delay = _number(retry_delay, "retry_delay", 0, 604_800)
        with self._connection(write=True) as connection:
            now = _now()
            row = connection.execute(
                "SELECT * FROM hub_jobs WHERE tenant_id=? AND id=? AND worker_id=? "
                "AND lease_token=? AND status='running' AND lease_until>?",
                (*identity, now),
            ).fetchone()
            if row is None:
                return False
            dead = row["attempts"] >= row["max_attempts"]
            connection.execute(
                "UPDATE hub_jobs SET status=?, error=?, worker_id=NULL, lease_until=NULL, "
                "not_before=?, updated_at=?, finished_at=? WHERE tenant_id=? AND id=?",
                (
                    "deadletter" if dead else "queued",
                    error,
                    now + delay,
                    now,
                    now if dead else None,
                    identity[0],
                    identity[1],
                ),
            )
            self._append_event(
                connection,
                identity[0],
                identity[1],
                "job.deadletter" if dead else "job.retry_scheduled",
                {
                    "attempts": row["attempts"],
                    "error": error,
                },
                now,
            )
            return True

    def get_job(self, tenant_id: str, job_id: str) -> JsonDict | None:
        tenant_id, job_id = _text(tenant_id, "tenant_id"), _text(job_id, "job_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM hub_jobs WHERE tenant_id=? AND id=?",
                (tenant_id, job_id),
            ).fetchone()
            return _job(row) if row is not None else None

    def list_jobs(self, tenant_id: str, limit: int = 100) -> list[JsonDict]:
        tenant_id, limit = _text(tenant_id, "tenant_id"), _integer(limit, "limit", 1, 1000)
        with self._connection() as connection:
            return [
                _job(row)
                for row in connection.execute(
                    "SELECT * FROM hub_jobs WHERE tenant_id=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (tenant_id, limit),
                ).fetchall()
            ]

    def event(
        self,
        tenant_id: str,
        job_id: str | None,
        event_type: str,
        payload: JsonDict,
    ) -> None:
        tenant_id = _text(tenant_id, "tenant_id")
        if job_id is not None:
            job_id = _text(job_id, "job_id")
        event_type = _text(event_type, "event_type")
        encoded = _json(payload)
        with self._connection(write=True) as connection:
            if job_id is not None:
                self._get(connection, tenant_id, job_id)
            connection.execute(
                "INSERT INTO hub_events(tenant_id, job_id, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (tenant_id, job_id, event_type, encoded, _now()),
            )

    def events(
        self,
        tenant_id: str,
        job_id: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        tenant_id, limit = _text(tenant_id, "tenant_id"), _integer(limit, "limit", 1, 1000)
        clause = ""
        params: list[object] = [tenant_id]
        if job_id is not None:
            clause = " AND job_id=?"
            params.append(_text(job_id, "job_id"))
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM hub_events WHERE tenant_id=?" + clause + " ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
            return [_event(row) for row in reversed(rows)]

    def put_schedule(
        self,
        tenant_id: str,
        name: str,
        kind: str,
        payload: JsonDict,
        interval_seconds: float,
        *,
        enabled: bool = True,
    ) -> JsonDict:
        tenant_id = _text(tenant_id, "tenant_id")
        name, kind = _text(name, "name"), _text(kind, "kind")
        encoded = _json(payload)
        interval = _number(interval_seconds, "interval_seconds", 1, 31_536_000)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        with self._connection(write=True) as connection:
            now = _now()
            row = connection.execute(
                "SELECT * FROM hub_schedules WHERE tenant_id=? AND name=?",
                (tenant_id, name),
            ).fetchone()
            if row is None:
                schedule_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO hub_schedules(id,tenant_id,name,kind,payload_json,interval_seconds,"
                    "enabled,next_run_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        schedule_id,
                        tenant_id,
                        name,
                        kind,
                        encoded,
                        interval,
                        int(enabled),
                        now,
                        now,
                        now,
                    ),
                )
            else:
                schedule_id = row["id"]
                # Edits preserve an already-enqueued bucket's immutable snapshot.
                due = (
                    now
                    if interval != row["interval_seconds"] or (enabled and not row["enabled"])
                    else row["next_run_at"]
                )
                connection.execute(
                    "UPDATE hub_schedules SET kind=?,payload_json=?,interval_seconds=?,enabled=?,"
                    "next_run_at=?,updated_at=? WHERE tenant_id=? AND id=?",
                    (kind, encoded, interval, int(enabled), due, now, tenant_id, schedule_id),
                )
            self._append_event(
                connection,
                tenant_id,
                None,
                "schedule.updated" if row else "schedule.created",
                {"schedule_id": schedule_id, "name": name, "enabled": enabled},
                now,
            )
            return _schedule(
                connection.execute(
                    "SELECT * FROM hub_schedules WHERE tenant_id=? AND id=?",
                    (tenant_id, schedule_id),
                ).fetchone()
            )

    def tick_schedules(self, tenant_id: str, now: float | None = None) -> int:
        tenant_id = _text(tenant_id, "tenant_id")
        created = 0
        with self._connection(write=True) as connection:
            timestamp = _now() if now is None else _number(now, "now", 0, _MAX_TIMESTAMP)
            schedules = connection.execute(
                "SELECT * FROM hub_schedules WHERE tenant_id=? AND enabled=1 AND next_run_at<=? "
                "ORDER BY next_run_at,id",
                (tenant_id, timestamp),
            ).fetchall()
            for schedule in schedules:
                bucket = math.floor(timestamp / schedule["interval_seconds"])
                # Include the interval in the bucket identity so interval edits do not
                # collide with unrelated historical bucket numbers.
                key = f"schedule:{schedule['id']}:{schedule['interval_seconds']}:{bucket}"
                exists = connection.execute(
                    "SELECT 1 FROM hub_jobs WHERE tenant_id=? AND idempotency_key=?",
                    (tenant_id, key),
                ).fetchone()
                if exists is None:
                    job = self._enqueue(
                        connection,
                        tenant_id,
                        schedule["kind"],
                        schedule["payload_json"],
                        key,
                        timestamp,
                        3,
                        timestamp,
                    )
                    self._append_event(
                        connection,
                        tenant_id,
                        str(job["id"]),
                        "schedule.enqueued",
                        {
                            "schedule_id": schedule["id"],
                            "bucket": bucket,
                        },
                        timestamp,
                    )
                    created += 1
                connection.execute(
                    "UPDATE hub_schedules SET next_run_at=?,last_run_at=?,updated_at=? "
                    "WHERE tenant_id=? AND id=?",
                    (
                        (bucket + 1) * schedule["interval_seconds"],
                        timestamp,
                        timestamp,
                        tenant_id,
                        schedule["id"],
                    ),
                )
        return created

    def list_schedules(self, tenant_id: str) -> list[JsonDict]:
        tenant_id = _text(tenant_id, "tenant_id")
        with self._connection() as connection:
            return [
                _schedule(row)
                for row in connection.execute(
                    "SELECT * FROM hub_schedules WHERE tenant_id=? ORDER BY name",
                    (tenant_id,),
                ).fetchall()
            ]

    def summary(self, tenant_id: str) -> JsonDict:
        tenant_id = _text(tenant_id, "tenant_id")
        with self._connection() as connection:
            counts = dict.fromkeys(_STATUSES, 0)
            for row in connection.execute(
                "SELECT status,COUNT(*) AS total FROM hub_jobs WHERE tenant_id=? GROUP BY status",
                (tenant_id,),
            ).fetchall():
                counts[row["status"]] = row["total"]
            schedules = connection.execute(
                "SELECT COUNT(*) AS total,COALESCE(SUM(enabled),0) AS enabled "
                "FROM hub_schedules WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()
            events = connection.execute(
                "SELECT COUNT(*) FROM hub_events WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()[0]
            return {
                "tenant_id": tenant_id,
                "counts": counts,
                "total_jobs": sum(counts.values()),
                "enabled_schedules": schedules["enabled"],
                "total_schedules": schedules["total"],
                "total_events": events,
            }

    def _enqueue(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        kind: str,
        payload_json: str,
        key: str,
        initial_not_before: float | None,
        max_attempts: int,
        now: float,
    ) -> JsonDict:
        existing = connection.execute(
            "SELECT * FROM hub_jobs WHERE tenant_id=? AND idempotency_key=?",
            (tenant_id, key),
        ).fetchone()
        if existing is not None:
            if (
                existing["kind"],
                existing["payload_json"],
                existing["initial_not_before"],
                existing["max_attempts"],
            ) != (kind, payload_json, initial_not_before, max_attempts):
                raise ValueError("idempotency key was already used for a different request")
            return _job(existing)
        job_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO hub_jobs(id,tenant_id,kind,payload_json,idempotency_key,initial_not_before,"
            "not_before,status,max_attempts,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'queued',?,?,?)",
            (
                job_id,
                tenant_id,
                kind,
                payload_json,
                key,
                initial_not_before,
                initial_not_before if initial_not_before is not None else now,
                max_attempts,
                now,
                now,
            ),
        )
        self._append_event(connection, tenant_id, job_id, "job.queued", {"kind": kind}, now)
        return self._get(connection, tenant_id, job_id)

    def _expire(self, connection: sqlite3.Connection, tenant_id: str, now: float) -> None:
        expired = connection.execute(
            "SELECT * FROM hub_jobs WHERE tenant_id=? AND status='running' AND lease_until<=?",
            (tenant_id, now),
        ).fetchall()
        for row in expired:
            dead = row["attempts"] >= row["max_attempts"]
            connection.execute(
                "UPDATE hub_jobs SET status=?,worker_id=NULL,lease_until=NULL,not_before=?,"
                "updated_at=?,finished_at=?,error='worker lease expired' WHERE tenant_id=? AND id=?",
                (
                    "deadletter" if dead else "queued",
                    now,
                    now,
                    now if dead else None,
                    tenant_id,
                    row["id"],
                ),
            )
            self._append_event(
                connection,
                tenant_id,
                row["id"],
                "job.lease_expired",
                {
                    "worker_id": row["worker_id"],
                    "lease_token": row["lease_token"],
                    "attempts": row["attempts"],
                },
                now,
            )
            if dead:
                self._append_event(
                    connection,
                    tenant_id,
                    row["id"],
                    "job.deadletter",
                    {"attempts": row["attempts"], "reason": "lease_expired"},
                    now,
                )

    @staticmethod
    def _get(connection: sqlite3.Connection, tenant_id: str, job_id: str) -> JsonDict:
        row = connection.execute(
            "SELECT * FROM hub_jobs WHERE tenant_id=? AND id=?",
            (tenant_id, job_id),
        ).fetchone()
        if row is None:
            raise KeyError("job not found")
        return _job(row)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        tenant_id: str,
        job_id: str | None,
        event_type: str,
        payload: JsonDict,
        now: float,
    ) -> None:
        connection.execute(
            "INSERT INTO hub_events(tenant_id,job_id,event_type,payload_json,created_at) VALUES (?,?,?,?,?)",
            (tenant_id, job_id, event_type, _json(payload), now),
        )

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _text(value: str, name: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name} must contain 1 to {maximum} nonblank characters without NUL")
    return value.strip()


def _number(value: float, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number between {minimum} and {maximum}")
    try:
        numeric = float(value)
    except OverflowError:
        raise ValueError(f"{name} is outside the supported range") from None
    if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
        raise ValueError(f"{name} must be a finite number between {minimum} and {maximum}")
    return numeric


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _identity(
    tenant_id: str, job_id: str, worker_id: str, lease_token: int
) -> tuple[str, str, str, int]:
    return (
        _text(tenant_id, "tenant_id"),
        _text(job_id, "job_id"),
        _text(worker_id, "worker_id"),
        _integer(lease_token, "lease_token", 1, 2**63 - 1),
    )


def _json(value: JsonDict) -> str:
    if not isinstance(value, dict):
        raise ValueError("payload/result must be a JSON object")

    def validate(item: object, depth: int = 0) -> None:
        if depth > 32:
            raise ValueError("JSON nesting exceeds 32 levels")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                validate(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("payload/result contains a non-JSON value")

    try:
        validate(value)
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        size = len(encoded.encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise ValueError(f"invalid JSON object: {error}") from None
    if size > _MAX_JSON_BYTES:
        raise ValueError("JSON object exceeds 1 MiB")
    return encoded


def _now() -> float:
    return _number(time.time(), "current time", 0, _MAX_TIMESTAMP)


def _job(row: sqlite3.Row) -> JsonDict:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    encoded = result.pop("result_json")
    result["result"] = json.loads(encoded) if encoded is not None else None
    return cast(JsonDict, result)


def _event(row: sqlite3.Row) -> JsonDict:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return cast(JsonDict, result)


def _schedule(row: sqlite3.Row) -> JsonDict:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    result["enabled"] = bool(result["enabled"])
    return cast(JsonDict, result)
