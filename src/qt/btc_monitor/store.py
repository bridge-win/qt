"""Single-owner, durable monitor persistence, isolated from the asyncio ingest loop.

SQLite is owned exclusively by a daemon writer thread. An operating-system lock
remains held until that thread has actually closed the database, even if bounded
shutdown times out. JSON readers never open the database.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, InvalidStateError
from datetime import datetime
from pathlib import Path
from typing import Any, cast


class StoreError(RuntimeError):
    """A safe, fixed-code storage failure; never contains persisted payloads."""


def timestamp(value: Any, *, default: float | None = None) -> float:
    """Normalize persisted UTC times without accepting NaN or infinity."""
    if value is None and default is not None:
        return default
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp_requires_timezone")
        result = value.timestamp()
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp_requires_timezone")
        result = parsed.timestamp()
    else:
        result = float(value)
    if not math.isfinite(result):
        raise ValueError("timestamp_must_be_finite")
    return result


def _json(value: Any) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


class SnapshotStore:
    """Bounded transactional revisions/outbox and an atomic ``latest.json``.

    ``persist`` accepts JSON-compatible dictionaries. A decision has ``event_id``
    (or ``decision_id``), ``revision``, and ``expires_at``. Notifications contain
    that identity plus ``notification_class``, ``text``, ``expires_at`` and an
    optional ``positive`` flag. All public I/O methods are asynchronous. Queue
    overflow is fatal: the collector must stop instead of losing a transition.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        queue_size: int = 16,
        retention_days: float = 7,
        max_decisions: int = 1000,
        max_outbox: int = 1000,
        min_free_bytes: int = 16 * 1024 * 1024,
        max_snapshot_bytes: int = 1024 * 1024,
        notification_stale_seconds: Mapping[str, float] | None = None,
        max_future_seconds: float = 2.0,
        max_notification_snapshot_age: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if min(queue_size, max_decisions, max_outbox, max_snapshot_bytes) < 1:
            raise ValueError("storage_bounds_must_be_positive")
        if retention_days <= 0 or min_free_bytes < 0:
            raise ValueError("invalid_storage_retention_or_watermark")
        self.state_dir = Path(state_dir)
        self.latest_path = self.state_dir / "latest.json"
        self.database_path = self.state_dir / "monitor.sqlite3"
        self._queue: queue.Queue[tuple[str, tuple[Any, ...], Future[Any]]] = queue.Queue(queue_size)
        self._retention_seconds = retention_days * 86400
        self._max_decisions = max_decisions
        self._max_outbox = max_outbox
        self._min_free_bytes = min_free_bytes
        self._max_snapshot_bytes = max_snapshot_bytes
        self._notification_stale_seconds = dict(
            notification_stale_seconds
            or {
                "trade": 5.0,
                "quote": 5.0,
                "candle": 125.0,
            }
        )
        if (
            set(self._notification_stale_seconds) != {"trade", "quote", "candle"}
            or any(
                not math.isfinite(value) or value <= 0
                for value in self._notification_stale_seconds.values()
            )
            or not math.isfinite(max_future_seconds)
            or max_future_seconds < 0
            or not math.isfinite(max_notification_snapshot_age)
            or max_notification_snapshot_age <= 0
        ):
            raise ValueError("invalid_notification_freshness_bounds")
        self._max_future_seconds = max_future_seconds
        self._max_notification_snapshot_age = max_notification_snapshot_age
        self._clock = clock
        self._thread: threading.Thread | None = None
        self._closing = False
        self._closed = threading.Event()
        self._failure: StoreError | None = None
        self._ready = False
        self._last_progress_at: float | None = None
        self._last_persist_at: float | None = None
        self._writes = 0
        self._db: sqlite3.Connection | None = None
        self._lock_file: Any = None
        self._latest_assessment_id: str | None = None
        self._assessment_barrier_revision = 0
        self._latest_projection: dict[str, Any] | None = None
        self._latest_persist_mono = 0.0

    async def start(self) -> None:
        if self._thread is not None:
            self._check()
            if self._ready:
                return
            raise StoreError("storage_already_started")
        future: Future[Any] = Future()
        self._thread = threading.Thread(
            target=self._worker, args=(future,), name="btc-monitor-store", daemon=True
        )
        self._thread.start()
        await asyncio.wrap_future(future)

    def _check(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._closing:
            raise StoreError("storage_closed")
        if self._thread is None:
            raise StoreError("storage_not_started")

    async def _request(self, operation: str, *args: Any) -> Any:
        self._check()
        future: Future[Any] = Future()
        try:
            self._queue.put_nowait((operation, args, future))
        except queue.Full:
            self._failure = StoreError("storage_queue_full")
            raise self._failure from None
        # The thread can fail between _check and put_nowait. Resolve locally if
        # its final drain already ran, rather than leaving an orphaned await.
        if self._failure is not None or self._closed.is_set():
            self._resolve(future, error=self._failure or StoreError("storage_closed"))
        return await asyncio.wrap_future(future)

    async def persist(
        self, snapshot: Mapping[str, Any], notifications: Sequence[Mapping[str, Any]] = ()
    ) -> None:
        # Serialize in the worker, but detach caller-owned mutable dictionaries.
        # The producer must not mutate nested values until this await completes.
        await self._request("persist", dict(snapshot), [dict(item) for item in notifications])

    async def flush(self) -> None:
        await self._request("flush")

    async def load_checkpoint(self) -> dict[str, Any] | None:
        """Read the durable identity checkpoint without restoring live readiness.

        SQLite commits before the JSON projection is replaced. Recovery must use
        this checkpoint if a crash left the public projection one revision older.
        """
        return cast(dict[str, Any] | None, await self._request("checkpoint"))

    async def fetch_outbox(self, limit: int = 16, now: float | None = None) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            await self._request("fetch", max(1, min(limit, self._max_outbox)), now),
        )

    async def current_revision(self, event_id: str) -> int | None:
        return cast(int | None, await self._request("revision", event_id))

    async def notification_is_current(self, item: Mapping[str, Any]) -> bool:
        return bool(await self._request("current", dict(item)))

    async def claim_outbox(self, item: Mapping[str, Any]) -> bool:
        """Atomically claim one still-current due item before a network attempt."""
        return bool(await self._request("claim", dict(item)))

    async def update_outbox(
        self,
        event_id: str,
        revision: int,
        notification_class: str,
        *,
        status: str,
        attempts: int,
        next_retry_at: float | None = None,
        error_code: str | None = None,
    ) -> None:
        if status not in {
            "pending",
            "sending",
            "accepted",
            "expired",
            "obsolete",
            "failed",
            "unknown",
        }:
            raise ValueError("invalid_outbox_status")
        if error_code not in {
            None,
            "rate_limited",
            "rate_limit_horizon",
            "attempt_limit",
            "transport_unknown",
            "telegram_rejected",
            "telegram_invalid_response",
            "interrupted_send",
        }:
            raise ValueError("invalid_notification_error_code")
        await self._request(
            "update",
            event_id,
            revision,
            notification_class,
            status,
            attempts,
            next_retry_at,
            error_code,
        )

    @property
    def failure(self) -> StoreError | None:
        return self._failure

    def health(self) -> dict[str, Any]:
        return {
            "ready": self._ready and self._failure is None and not self._closing,
            "worker_alive": bool(self._thread and self._thread.is_alive()),
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "last_progress_at": self._last_progress_at,
            "last_persist_at": self._last_persist_at,
            "last_persisted_at": self._last_persist_at,
            "writes": self._writes,
            "error": str(self._failure) if self._failure else None,
        }

    async def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._closing = True
        deadline = time.monotonic() + timeout
        while not self._closed.is_set():
            if time.monotonic() >= deadline:
                raise StoreError("storage_shutdown_timeout")
            await asyncio.sleep(min(0.02, max(0, deadline - time.monotonic())))
        if self._failure is not None:
            raise self._failure

    def _worker(self, ready: Future[Any]) -> None:
        try:
            self._initialize()
            self._ready = True
            self._last_progress_at = self._clock()
            self._resolve(ready, None)
            while self._failure is None:
                try:
                    operation, args, future = self._queue.get(timeout=0.02)
                except queue.Empty:
                    if self._closing:
                        break
                    continue
                try:
                    result = self._execute(operation, args)
                    self._last_progress_at = self._clock()
                    if self._failure is not None:
                        raise self._failure
                    self._resolve(future, result)
                except Exception as exc:
                    self._failure = self._failure or (
                        exc if isinstance(exc, StoreError) else StoreError("storage_write_failed")
                    )
                    self._resolve(future, error=self._failure)
                finally:
                    self._queue.task_done()
        except Exception as exc:
            self._failure = (
                exc if isinstance(exc, StoreError) else StoreError("storage_initialize_failed")
            )
            self._resolve(ready, error=self._failure)
        finally:
            self._ready = False
            while True:
                try:
                    _, _, future = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._resolve(future, error=self._failure or StoreError("storage_closed"))
                self._queue.task_done()
            try:
                if self._db is not None:
                    self._db.close()
            finally:
                if self._lock_file is not None:
                    self._lock_file.close()
                self._closed.set()

    @staticmethod
    def _resolve(future: Future[Any], value: Any = None, error: Exception | None = None) -> None:
        if future.cancelled() or future.done():
            return
        try:
            if error is None:
                future.set_result(value)
            else:
                future.set_exception(error)
        except InvalidStateError:
            pass  # The asyncio caller can cancel between the check and resolve.

    def _initialize(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self.state_dir / ".writer.lock").open("a+b")
        try:
            if os.name == "nt":  # pragma: no cover - Linux CI exercises flock.
                import importlib

                msvcrt = importlib.import_module("msvcrt")

                self._lock_file.seek(0)
                if self._lock_file.read(1) == b"":
                    self._lock_file.write(b"0")
                    self._lock_file.flush()
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise StoreError("storage_directory_in_use") from None
        os.fchmod(self._lock_file.fileno(), 0o640)
        self._check_free_space()
        self._db = sqlite3.connect(self.database_path, timeout=1)
        os.chmod(self.database_path, 0o640)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=DELETE")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                event_id TEXT NOT NULL, revision INTEGER NOT NULL, created_at REAL NOT NULL,
                status TEXT NOT NULL, expires_at REAL NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (event_id, revision)
            );
            CREATE TABLE IF NOT EXISTS outbox (
                event_id TEXT NOT NULL, revision INTEGER NOT NULL,
                notification_class TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, positive INTEGER NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
                error_code TEXT,
                PRIMARY KEY (event_id, revision, notification_class)
            );
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        # A crash may have happened after Telegram accepted a request. Never retry
        # interrupted sends: external exactly-once delivery cannot be guaranteed.
        self._db.execute(
            "UPDATE outbox SET status='unknown', error_code='interrupted_send' WHERE status='sending'"
        )
        self._db.commit()
        prior = self._load_checkpoint()
        if prior:
            self._latest_assessment_id = prior.get("assessment", {}).get("assessment_id")
        barrier = self._db.execute(
            "SELECT payload FROM state WHERE key='assessment_barrier'"
        ).fetchone()
        if barrier:
            saved = json.loads(barrier[0])
            if saved["assessment_id"] == self._latest_assessment_id:
                self._assessment_barrier_revision = saved["revision"]

    def _load_checkpoint(self) -> dict[str, Any] | None:
        assert self._db is not None
        length = self._db.execute(
            "SELECT length(CAST(payload AS BLOB)) FROM state WHERE key='latest'"
        ).fetchone()
        if length is None:
            return None
        if length[0] > self._max_snapshot_bytes:
            raise StoreError("storage_checkpoint_too_large")
        row = self._db.execute("SELECT payload FROM state WHERE key='latest'").fetchone()
        checkpoint = json.loads(row[0])
        if not isinstance(checkpoint, dict):
            raise StoreError("storage_checkpoint_invalid")
        _json(checkpoint)  # Reject non-finite JSON constants as on the write path.
        return checkpoint

    def _check_free_space(self) -> None:
        if shutil.disk_usage(self.state_dir).free < self._min_free_bytes:
            raise StoreError("storage_low_disk_space")

    def _execute(self, operation: str, args: tuple[Any, ...]) -> Any:
        assert self._db is not None
        if operation == "persist":
            return self._persist(*args)
        if operation == "flush":
            return None
        if operation == "checkpoint":
            return self._load_checkpoint()
        if operation == "fetch":
            limit, provided_now = args
            now = self._clock() if provided_now is None else provided_now
            with self._db:
                self._db.execute(
                    "UPDATE outbox SET status='expired' WHERE status='pending' AND expires_at<=?",
                    (now,),
                )
            rows = self._db.execute(
                "SELECT * FROM outbox WHERE status='pending' AND next_retry_at<=? "
                "ORDER BY created_at, rowid LIMIT ?",
                (now, limit),
            ).fetchall()
            result = []
            with self._db:
                for row in rows:
                    item = dict(row) | {"payload": json.loads(row["payload"])}
                    if not self._is_current(item, now=now):
                        self._db.execute(
                            "UPDATE outbox SET status='obsolete' WHERE event_id=? AND revision=? "
                            "AND notification_class=?",
                            (row["event_id"], row["revision"], row["notification_class"]),
                        )
                    else:
                        result.append(item)
            return result
        if operation == "revision":
            row = self._db.execute(
                "SELECT MAX(revision) FROM decisions WHERE event_id=?", args
            ).fetchone()
            return row[0]
        if operation == "current":
            return self._is_current(args[0])
        if operation == "claim":
            item = args[0]
            with self._db:
                if not self._is_current(item):
                    return False
                cursor = self._db.execute(
                    "UPDATE outbox SET status='sending',attempts=attempts+1,error_code=NULL "
                    "WHERE event_id=? AND revision=? AND notification_class=? AND status='pending' "
                    "AND next_retry_at<=? AND attempts=?",
                    (
                        item["event_id"],
                        item["revision"],
                        item["notification_class"],
                        self._clock(),
                        item["attempts"],
                    ),
                )
                return cursor.rowcount == 1
        if operation == "update":
            event_id, revision, kind, status, attempts, retry, error = args
            with self._db:
                self._db.execute(
                    "UPDATE outbox SET status=?, attempts=?, next_retry_at=?, error_code=? "
                    "WHERE event_id=? AND revision=? AND notification_class=?",
                    (status, attempts, retry or 0, error, event_id, revision, kind),
                )
            return None
        raise StoreError("storage_unknown_operation")

    def _is_current(self, item: Mapping[str, Any], *, now: float | None = None) -> bool:
        assert self._db is not None
        now = self._clock() if now is None else now
        if timestamp(item.get("expires_at")) <= now:
            return False
        if not item.get("positive", False):
            return True
        if not self._positive_eligible(now):
            return False
        row = self._db.execute(
            "SELECT revision,status,expires_at FROM decisions WHERE event_id=? ORDER BY revision DESC LIMIT 1",
            (item["event_id"],),
        ).fetchone()
        if not row or row["expires_at"] <= now:
            return False
        if row["status"] == "CANDIDATE":
            # Continuous candidate evidence revisions may advance while an
            # earlier notification waits. A durable withdrawal barrier prevents
            # re-entry into CANDIDATE from reviving a pre-withdrawal positive.
            return bool(
                item["event_id"] == self._latest_assessment_id
                and self._assessment_barrier_revision < item["revision"] <= row["revision"]
            )
        return bool(row["status"] == "CONFIRMED" and row["revision"] == item["revision"])

    def _positive_eligible(self, now: float) -> bool:
        projection = self._latest_projection
        if projection is None:
            return False  # Historical readiness is never restored on startup.
        try:
            health = projection["health"]
            if health.get("data_ready") is not True:
                return False
            elapsed = now - timestamp(projection["published_at"])
            if elapsed < -self._max_future_seconds:
                return False
            elapsed = max(0.0, elapsed, time.monotonic() - self._latest_persist_mono)
            if elapsed > self._max_notification_snapshot_age:
                return False
            channels = health["channels"]
            for name, stale in self._notification_stale_seconds.items():
                channel = channels[name]
                if channel.get("ready") is not True:
                    return False
                if name != "candle":
                    receive_age = timestamp(channel["receive_age_seconds"])
                    if receive_age < 0 or receive_age + elapsed > stale:
                        return False
                source_age = channel.get("event_age_seconds")
                if source_age is None:
                    if name != "quote" or channel.get("source_time_known", True):
                        return False
                else:
                    age = timestamp(source_age)
                    if age < -self._max_future_seconds or age + elapsed > stale:
                        return False
            return True
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def _persist(self, snapshot: dict[str, Any], notifications: list[dict[str, Any]]) -> None:
        assert self._db is not None
        self._check_free_space()
        encoded = _json(snapshot)
        if len(encoded.encode("utf-8")) > self._max_snapshot_bytes:
            raise StoreError("storage_snapshot_too_large")
        now = self._clock()
        decisions = list(snapshot.get("recent_events", []))
        decisions.extend(
            value
            for key in ("decision", "assessment")
            if isinstance(value := snapshot.get(key), dict)
        )
        assessment = snapshot.get("assessment", {})
        assessment_id = assessment.get("assessment_id")
        barrier_revision = (
            self._assessment_barrier_revision if assessment_id == self._latest_assessment_id else 0
        )
        if assessment_id and assessment.get("state") != "CANDIDATE":
            barrier_revision = int(assessment["revision"])
        with self._db:
            for decision in decisions:
                event_id = (
                    decision.get("event_id")
                    or decision.get("decision_id")
                    or decision.get("assessment_id")
                )
                if event_id:
                    payload = _json(decision)
                    existing = self._db.execute(
                        "SELECT payload FROM decisions WHERE event_id=? AND revision=?",
                        (str(event_id), int(decision["revision"])),
                    ).fetchone()
                    if existing and existing[0] != payload:
                        raise StoreError("storage_revision_conflict")
                    self._db.execute(
                        "INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?)",
                        (
                            str(event_id),
                            int(decision["revision"]),
                            now,
                            str(decision.get("state", decision.get("status", "unknown"))).upper(),
                            timestamp(decision.get("expires_at"), default=now),
                            payload,
                        ),
                    )
            for item in notifications:
                event_id = str(item.get("event_id") or item["decision_id"])
                kind = str(item["notification_class"])
                text = str(item["text"])
                if not text or len(text) > 4096 or len(_json(item).encode("utf-8")) > 16384:
                    raise StoreError("storage_notification_too_large")
                normalized = dict(item, event_id=event_id)
                self._db.execute(
                    "INSERT OR IGNORE INTO outbox "
                    "(event_id,revision,notification_class,created_at,expires_at,positive,payload) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        event_id,
                        int(item["revision"]),
                        kind,
                        now,
                        timestamp(item["expires_at"]),
                        int(bool(item.get("positive", False))),
                        _json(normalized),
                    ),
                )
            self._db.execute(
                "INSERT INTO state VALUES ('latest', ?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (encoded,),
            )
            self._db.execute(
                "INSERT INTO state VALUES ('assessment_barrier', ?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (_json({"assessment_id": assessment_id, "revision": barrier_revision}),),
            )
            self._retain(now)
        self._latest_assessment_id = assessment_id
        self._assessment_barrier_revision = barrier_revision
        self._atomic_export(encoded)
        self._latest_projection = json.loads(encoded)
        self._latest_persist_mono = time.monotonic()
        self._last_persist_at = self._clock()
        self._writes += 1

    def _retain(self, now: float) -> None:
        assert self._db is not None
        self._db.execute(
            "UPDATE outbox SET status='expired' WHERE status='pending' AND expires_at<=?", (now,)
        )
        self._db.execute(
            "DELETE FROM outbox WHERE status NOT IN ('pending','sending') AND created_at<?",
            (now - self._retention_seconds,),
        )
        count = self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        if count > self._max_outbox:
            self._db.execute(
                "DELETE FROM outbox WHERE rowid IN (SELECT rowid FROM outbox "
                "WHERE status NOT IN ('pending','sending') ORDER BY created_at, rowid LIMIT ?)",
                (count - self._max_outbox,),
            )
        if self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] > self._max_outbox:
            raise StoreError("storage_outbox_capacity")
        self._db.execute(
            "DELETE FROM decisions WHERE created_at<?", (now - self._retention_seconds,)
        )
        self._db.execute(
            "DELETE FROM decisions WHERE rowid NOT IN "
            "(SELECT rowid FROM decisions ORDER BY created_at DESC, rowid DESC LIMIT ?)",
            (self._max_decisions,),
        )

    def _atomic_export(self, encoded: str) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".latest-",
                suffix=".tmp",
                dir=self.state_dir,
                delete=False,
            ) as handle:
                temporary = handle.name
                os.fchmod(handle.fileno(), 0o640)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.latest_path)
            temporary = None
            if os.name != "nt":
                directory_fd = os.open(self.state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
