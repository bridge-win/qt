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
        now = self._timestamp()
        job_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            queued = cast(
                int,
                connection.execute(
                    "SELECT count(*) FROM research_jobs "
                    "WHERE status IN ('queued', 'running')"
                ).fetchone()[0],
            )
            if queued >= self.queue_limit:
                raise ValueError(
                    f"backtest queue is full: {queued} queued/running"
                )
            connection.execute(
                """
                INSERT INTO research_jobs (
                    job_id, spec_json, status, stage, progress,
                    cancel_requested, attempts, created_at, updated_at
                ) VALUES (?, ?, 'queued', 'queued', 0, 0, 0, ?, ?)
                """,
                (job_id, _json(spec), now, now),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JsonDict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM research_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _job_view(row)

    def claim_next(self, worker_id: str) -> JsonDict | None:
        now = self._timestamp()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
                    updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (worker_id, now, now, job_id),
            )
        return self.get_job(job_id)

    def update_progress(self, job_id: str, stage: str, progress: int) -> JsonDict:
        bounded = max(0, min(progress, 99))
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE research_jobs
                SET stage = ?, progress = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running'
                """,
                (stage, bounded, self._timestamp(), job_id),
            )
        return self.get_job(job_id)

    def complete(self, job_id: str, result: Mapping[str, object]) -> JsonDict:
        now = self._timestamp()
        run_id = str(result.get("run_id", "")).strip()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE research_jobs
                SET status = 'complete', stage = 'complete', progress = 100,
                    result_json = ?, updated_at = ?, completed_at = ?
                WHERE job_id = ? AND status = 'running'
                    AND cancel_requested = 0
                """,
                (_json(result), now, now, job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "research job cannot complete unless it is running "
                    "without cancellation"
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

    def fail(self, job_id: str, error: str) -> JsonDict:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE research_jobs
                SET status = 'failed', stage = 'failed', error = ?,
                    updated_at = ?, completed_at = ?
                WHERE job_id = ?
                """,
                (error[:4000], self._timestamp(), self._timestamp(), job_id),
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
                    "UPDATE research_jobs SET cancel_requested = 1, updated_at = ? "
                    "WHERE job_id = ? AND status = 'running'",
                    (now, job_id),
                )
        return self.get_job(job_id)

    def cancel_running(self, job_id: str) -> JsonDict:
        now = self._timestamp()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE research_jobs
                SET status = 'cancelled', stage = 'cancelled', progress = 100,
                    updated_at = ?, completed_at = ?
                WHERE job_id = ? AND status = 'running'
                """,
                (now, now, job_id),
            )
        return self.get_job(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        return bool(self.get_job(job_id)["cancel_requested"])

    def recover_stale(self, *, stale_after: timedelta) -> int:
        cutoff = (self._clock() - stale_after).astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE research_jobs
                SET status = 'queued', stage = 'queued', progress = 0,
                    claimed_by = NULL, claimed_at = NULL, updated_at = ?
                WHERE status = 'running' AND claimed_at < ? AND attempts < 2
                """,
                (self._timestamp(), cutoff),
            )
            connection.execute(
                """
                UPDATE research_jobs
                SET status = 'failed', stage = 'failed',
                    error = 'worker interrupted repeatedly',
                    updated_at = ?, completed_at = ?
                WHERE status = 'running' AND claimed_at < ? AND attempts >= 2
                """,
                (self._timestamp(), self._timestamp(), cutoff),
            )
            return max(cursor.rowcount, 0)

    def list_runs(self, *, limit: int = 50) -> list[JsonDict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT summary_json FROM research_runs "
                "ORDER BY created_at DESC LIMIT ?",
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
            "running": counts.get("running", 0),
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
                "WHERE status IN ('queued', 'running')"
            ).fetchall()
        dataset_ids: set[str] = set()
        for row in rows:
            spec = _object(str(row["spec_json"]))
            if spec.get("kind") == "dataset_sync":
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
                """
            )

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


def _job_view(row: sqlite3.Row) -> JsonDict:
    return {
        "job_id": str(row["job_id"]),
        "spec": _object(str(row["spec_json"])),
        "status": str(row["status"]),
        "stage": str(row["stage"]),
        "progress": int(row["progress"]),
        "result": (
            _object(str(row["result_json"]))
            if row["result_json"] is not None
            else None
        ),
        "error": row["error"],
        "cancel_requested": bool(row["cancel_requested"]),
        "attempts": int(row["attempts"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "completed_at": row["completed_at"],
    }
