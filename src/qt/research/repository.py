"""SQLite WAL repository for durable research jobs and run indexes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeAlias, cast

JsonDict: TypeAlias = dict[str, object]
Clock: TypeAlias = Callable[[], datetime]

# This is intentionally a small, closed dispatch vocabulary.  Executors own
# their job-specific schemas; the queue owns lifecycle, admission, leases and
# cancellation for every kind of long-running work.
JOB_TYPES = frozenset(
    {
        "native_experiment",
        "lab_optimization",
        "lab_validation",
        "data_import",
        "data_sync",
    }
)
ACTIVE_STATUSES = ("queued", "running", "cancelling")


class IdempotencyConflictError(ValueError):
    """The client reused a key for a different request payload."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchRepository:
    def __init__(
        self,
        path: Path,
        *,
        queue_limit: int = 5,
        clock: Clock = _utc_now,
    ) -> None:
        if queue_limit < 1:
            raise ValueError("queue_limit must be positive")
        self.path = path
        self.queue_limit = queue_limit
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def enqueue(self, spec: Mapping[str, object]) -> JsonDict:
        normalized = _normalize_spec(spec)
        now = self._timestamp()
        job_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            queued = cast(
                int,
                connection.execute(
                    "SELECT count(*) FROM research_jobs "
                    "WHERE status IN ('queued', 'running', 'cancelling')"
                ).fetchone()[0],
            )
            if queued >= self.queue_limit:
                raise ValueError(f"backtest queue is full: {queued} queued/running")
            connection.execute(
                """
                INSERT INTO research_jobs (
                    job_id, spec_json, status, stage, progress,
                    cancel_requested, attempts, created_at, updated_at
                ) VALUES (?, ?, 'queued', 'queued', 0, 0, 0, ?, ?)
                """,
                (job_id, _json(normalized), now, now),
            )
        return self.get_job(job_id)

    def enqueue_idempotent(
        self,
        spec: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> tuple[JsonDict, bool]:
        """Atomically create or return exactly one durable research job."""

        key = idempotency_key.strip()
        if not key:
            raise ValueError("idempotency key must not be blank")
        encoded = _json(_normalize_spec(spec))
        now = self._timestamp()
        job_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT job_id, spec_json FROM research_submission_keys WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                if str(existing["spec_json"]) != encoded:
                    raise IdempotencyConflictError(
                        "idempotency key was already used with a different request"
                    )
                return self.get_job(str(existing["job_id"])), False
            queued = cast(
                int,
                connection.execute(
                    "SELECT count(*) FROM research_jobs "
                    "WHERE status IN ('queued', 'running', 'cancelling')"
                ).fetchone()[0],
            )
            if queued >= self.queue_limit:
                raise ValueError(f"backtest queue is full: {queued} queued/running")
            connection.execute(
                """
                INSERT INTO research_jobs (
                    job_id, spec_json, status, stage, progress,
                    cancel_requested, attempts, created_at, updated_at
                ) VALUES (?, ?, 'queued', 'queued', 0, 0, 0, ?, ?)
                """,
                (job_id, encoded, now, now),
            )
            connection.execute(
                """
                INSERT INTO research_submission_keys (idempotency_key, job_id, spec_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (key, job_id, encoded, now),
            )
        return self.get_job(job_id), True

    def lookup_idempotent(
        self,
        spec: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> JsonDict | None:
        """Return an existing identical request before admission work.

        This is deliberately a lookup only; ``enqueue_idempotent`` remains the
        transactional authority for the create-or-return race.
        """

        key = idempotency_key.strip()
        if not key:
            raise ValueError("idempotency key must not be blank")
        encoded = _json(_normalize_spec(spec))
        with self._connection() as connection:
            row = connection.execute(
                "SELECT job_id, spec_json FROM research_submission_keys WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        if str(row["spec_json"]) != encoded:
            raise IdempotencyConflictError(
                "idempotency key was already used with a different request"
            )
        return self.get_job(str(row["job_id"]))

    def get_job(self, job_id: str) -> JsonDict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM research_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _job_view(row)

    def list_jobs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        job_types: set[str] | None = None,
        statuses: set[str] | None = None,
    ) -> list[JsonDict]:
        """Return bounded, newest-first queue history for the v3 workbench."""

        bounded_limit = max(1, min(limit, 200))
        if offset < 0:
            raise ValueError("offset must not be negative")
        predicates: list[str] = []
        params: list[object] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            predicates.append(f"status IN ({placeholders})")
            params.extend(sorted(statuses))
        # Job type remains in immutable JSON for backwards-compatible SQLite
        # migrations.  JSON1 is part of supported SQLite builds; filtering in
        # Python keeps older system SQLite builds usable too.
        query = "SELECT * FROM research_jobs"
        if predicates:
            query += " WHERE " + " AND ".join(predicates)
        query += " ORDER BY created_at DESC, job_id DESC LIMIT ? OFFSET ?"
        params.extend((bounded_limit, offset))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        jobs = [_job_view(row) for row in rows]
        if job_types is None:
            return jobs
        unknown = job_types.difference(JOB_TYPES)
        if unknown:
            raise ValueError(f"unknown job types: {', '.join(sorted(unknown))}")
        return [job for job in jobs if job["job_type"] in job_types]

    def claim_next(self, worker_id: str) -> JsonDict | None:
        now = self._timestamp()
        attempt_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM research_jobs WHERE status IN ('running', 'cancelling') LIMIT 1"
                ).fetchone()
                is not None
            ):
                return None
            row = connection.execute(
                "SELECT job_id FROM research_jobs "
                "WHERE status = 'queued' ORDER BY created_at, job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["job_id"])
            connection.execute(
                """
                UPDATE research_jobs
                SET status = 'running', stage = 'validation', progress = 5,
                    attempts = attempts + 1, claimed_by = ?, claimed_at = ?,
                    attempt_id = ?, attempt_heartbeat_at = ?,
                    updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (worker_id, now, attempt_id, now, now, job_id),
            )
        return self.get_job(job_id)

    def update_progress(
        self, job_id: str, stage: str, progress: int, *, attempt_id: str | None = None
    ) -> JsonDict:
        bounded = max(0, min(progress, 99))
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE research_jobs
                SET stage = ?, progress = ?, updated_at = ?, attempt_heartbeat_at = ?
                WHERE job_id = ? AND status = 'running' AND (? IS NULL OR attempt_id = ?)
                """,
                (
                    stage,
                    bounded,
                    self._timestamp(),
                    self._timestamp(),
                    job_id,
                    attempt_id,
                    attempt_id,
                ),
            )
        return self.get_job(job_id)

    def heartbeat_attempt(self, job_id: str, *, attempt_id: str) -> bool:
        """Renew an active execution lease without changing its progress stage."""

        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE research_jobs SET attempt_heartbeat_at = ?, updated_at = ?
                WHERE job_id = ? AND status IN ('running', 'cancelling') AND attempt_id = ?
                """,
                (self._timestamp(), self._timestamp(), job_id, attempt_id),
            )
        return cursor.rowcount == 1

    def complete(
        self, job_id: str, result: Mapping[str, object], *, attempt_id: str | None = None
    ) -> JsonDict:
        now = self._timestamp()
        run_id = str(result.get("run_id", "")).strip()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE research_jobs
                SET status = 'complete', stage = 'complete', progress = 100,
                    result_json = ?, updated_at = ?, completed_at = ?
                WHERE job_id = ? AND status = 'running' AND cancel_requested = 0
                    AND (? IS NULL OR attempt_id = ?)
                """,
                (_json(result), now, now, job_id, attempt_id, attempt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "research job cannot complete unless it is running without cancellation"
                )
            if run_id:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO research_runs (
                        run_id, job_id, summary_json, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, job_id, _json(result), now),
                )
        return self.get_job(job_id)

    def fail(self, job_id: str, error: str, *, attempt_id: str | None = None) -> JsonDict:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE research_jobs
                SET status = 'failed', stage = 'failed', error = ?,
                    updated_at = ?, completed_at = ?
                WHERE job_id = ? AND status IN ('running', 'cancelling')
                    AND (? IS NULL OR attempt_id = ?)
                """,
                (
                    error[:4000],
                    self._timestamp(),
                    self._timestamp(),
                    job_id,
                    attempt_id,
                    attempt_id,
                ),
            )
        return self.get_job(job_id)

    def request_cancel(self, job_id: str) -> JsonDict:
        job = self.get_job(job_id)
        status = str(job["status"])
        now = self._timestamp()
        with self._connection() as connection:
            if status == "queued":
                connection.execute(
                    """
                    UPDATE research_jobs
                    SET status = 'cancelled', stage = 'cancelled',
                        cancel_requested = 1, updated_at = ?, completed_at = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (now, now, job_id),
                )
            elif status == "running":
                connection.execute(
                    "UPDATE research_jobs SET status = 'cancelling', stage = 'cancelling', "
                    "cancel_requested = 1, updated_at = ? "
                    "WHERE job_id = ? AND status = 'running'",
                    (now, job_id),
                )
        return self.get_job(job_id)

    def cancel_running(self, job_id: str, *, attempt_id: str | None = None) -> JsonDict:
        now = self._timestamp()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE research_jobs
                SET status = 'cancelled', stage = 'cancelled', progress = 100,
                    updated_at = ?, completed_at = ?
                WHERE job_id = ? AND status IN ('running', 'cancelling')
                    AND (? IS NULL OR attempt_id = ?)
                """,
                (now, now, job_id, attempt_id, attempt_id),
            )
        return self.get_job(job_id)

    def is_cancel_requested(self, job_id: str, *, attempt_id: str | None = None) -> bool:
        job = self.get_job(job_id)
        return bool(job["cancel_requested"]) or (
            attempt_id is not None and job.get("attempt_id") != attempt_id
        )

    def recover_stale(self, *, stale_after: timedelta) -> int:
        cutoff = (self._clock() - stale_after).astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE research_jobs
                SET status = 'queued', stage = 'queued', progress = 0,
                    claimed_by = NULL, claimed_at = NULL, attempt_id = NULL,
                    attempt_heartbeat_at = NULL, updated_at = ?
                WHERE status IN ('running', 'cancelling')
                    AND attempt_heartbeat_at < ? AND attempts < 2
                """,
                (self._timestamp(), cutoff),
            )
            connection.execute(
                """
                UPDATE research_jobs
                SET status = 'interrupted', stage = 'interrupted',
                    error = 'worker interrupted repeatedly',
                    updated_at = ?, completed_at = ?
                WHERE status IN ('running', 'cancelling')
                    AND attempt_heartbeat_at < ? AND attempts >= 2
                """,
                (self._timestamp(), self._timestamp(), cutoff),
            )
            return max(cursor.rowcount, 0)

    def list_runs(self, *, limit: int = 50) -> list[JsonDict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT summary_json FROM research_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [_object(row["summary_json"]) for row in rows]

    def get_run(self, run_id: str) -> JsonDict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT summary_json FROM research_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _object(row["summary_json"])

    def queue_counts(self) -> JsonDict:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, count(*) AS count FROM research_jobs GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0) + counts.get("cancelling", 0),
            "cancelling": counts.get("cancelling", 0),
            "queue_limit": self.queue_limit,
        }

    def submissions_since(self, since: datetime) -> int:
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("submission cutoff must be timezone-aware")
        cutoff = since.astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            value = connection.execute(
                "SELECT count(*) FROM research_jobs WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()[0]
        return int(value)

    def heartbeat(self, worker_id: str) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO research_workers (worker_id, heartbeat_at)
                VALUES (?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at
                """,
                (worker_id.strip(), self._timestamp()),
            )

    def worker_status(self, *, online_within: timedelta) -> JsonDict:
        cutoff = (self._clock() - online_within).astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT worker_id, heartbeat_at FROM research_workers "
                "ORDER BY heartbeat_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return {"online": False, "worker_id": None, "heartbeat_at": None}
        heartbeat = str(row["heartbeat_at"])
        return {
            "online": heartbeat >= cutoff,
            "worker_id": str(row["worker_id"]),
            "heartbeat_at": heartbeat,
        }

    def syncing_dataset_ids(self) -> set[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT spec_json FROM research_jobs "
                "WHERE status IN ('queued', 'running', 'cancelling')"
            ).fetchall()
        dataset_ids: set[str] = set()
        for row in rows:
            spec = _object(str(row["spec_json"]))
            if spec.get("job_type") == "data_sync" or spec.get("kind") == "dataset_sync":
                dataset_id = spec.get("dataset_id")
                if isinstance(dataset_id, str):
                    dataset_ids.add(dataset_id)
        return dataset_ids

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_jobs (
                    job_id TEXT PRIMARY KEY,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    attempt_id TEXT,
                    attempt_heartbeat_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_research_jobs_queue
                    ON research_jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    summary_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_workers (
                    worker_id TEXT PRIMARY KEY,
                    heartbeat_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_submission_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    spec_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES research_jobs(job_id)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(research_jobs)").fetchall()
            }
            if "attempt_id" not in columns:
                connection.execute("ALTER TABLE research_jobs ADD COLUMN attempt_id TEXT")
            if "attempt_heartbeat_at" not in columns:
                connection.execute("ALTER TABLE research_jobs ADD COLUMN attempt_heartbeat_at TEXT")

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("repository clock must return an aware datetime")
        return value.astimezone(timezone.utc).isoformat()


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _object(value: str) -> JsonDict:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("stored JSON must be an object")
    return cast(JsonDict, parsed)


def _normalize_spec(spec: Mapping[str, object]) -> JsonDict:
    """Attach the v3 job discriminator without breaking legacy queue callers."""

    normalized = dict(spec)
    job_type = normalized.get("job_type")
    if job_type is None:
        # The existing dashboard's dataset sync job is the only legacy
        # non-experiment producer.  Preserve its request shape while routing
        # it through the common worker lifecycle.
        job_type = "data_sync" if normalized.get("kind") == "dataset_sync" else "native_experiment"
        normalized["job_type"] = job_type
    if not isinstance(job_type, str) or job_type not in JOB_TYPES:
        raise ValueError(f"unknown job_type: {job_type!r}")
    return normalized


def _job_view(row: sqlite3.Row) -> JsonDict:
    spec = _object(str(row["spec_json"]))
    legacy_status = str(row["status"])
    return {
        "job_id": str(row["job_id"]),
        "spec": spec,
        "job_type": str(spec["job_type"]),
        "status": legacy_status,
        "v3_status": _v3_status(legacy_status),
        "stage": str(row["stage"]),
        "progress": int(row["progress"]),
        "result": (_object(str(row["result_json"])) if row["result_json"] is not None else None),
        "error": row["error"],
        "cancel_requested": bool(row["cancel_requested"]),
        "attempts": int(row["attempts"]),
        "attempt_id": row["attempt_id"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "completed_at": row["completed_at"],
    }


def _v3_status(status: str) -> str:
    """Map the v2 repository spelling while retaining existing callers."""

    return "succeeded" if status == "complete" else status
