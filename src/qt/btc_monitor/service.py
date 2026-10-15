"""Supervised public collector; disk and Telegram work never run inside ingest."""

from __future__ import annotations

import asyncio
import math
import os
import signal
import tempfile
import threading
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import MonitorConfig
from .engine import MonitorEngine
from .feed import run_feed
from .models import MarketEvent, MonitorSnapshot
from .notifications import AsyncTelegram
from .store import SnapshotStore


class MonitorService:
    def __init__(
        self,
        config: MonitorConfig,
        *,
        feed_runner: Callable[..., Coroutine[Any, Any, None]] = run_feed,
        store: SnapshotStore | None = None,
        fatal_exit: Callable[[int], None] = os._exit,
    ):
        self.config = config
        self.engine = MonitorEngine(config)
        self.store = store or SnapshotStore(
            config.state_dir,
            max_decisions=config.retention_events,
            max_outbox=config.retention_events,
            max_snapshot_bytes=1024 * 1024,
            notification_stale_seconds={
                "trade": config.trade_stale_seconds,
                "quote": config.quote_stale_seconds,
                "candle": config.candle_stale_seconds,
            },
            max_future_seconds=config.max_future_seconds,
        )
        self.queue: asyncio.Queue[MarketEvent] = asyncio.Queue(config.queue_size)
        self.stop_event = asyncio.Event()
        self.feed_runner = feed_runner
        self.last_consume_mono = time.monotonic()
        self.last_assess_mono = time.monotonic()
        self.persist_started_mono: float | None = None
        self.fatal_reason: str | None = None
        self._fatal_exit = fatal_exit
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self.notification_configured = bool(
            os.getenv("QT_TELEGRAM_BOT_TOKEN") and os.getenv("QT_TELEGRAM_CHAT_ID")
        )

    def emit(self, event: MarketEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Quote coalescing is optional; losing any trade path is never silent.
            self.engine.error("ingest_queue_overflow")
            self.reset("ingest_queue_overflow")

    def reset(self, reason: str) -> None:
        while not self.queue.empty():
            with suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
                self.queue.task_done()
        self.engine.reset(reason, time.time())

    async def _consume(self) -> None:
        while not self.stop_event.is_set():
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            self.engine.ingest(item, time.time(), time.monotonic())
            self.last_consume_mono = time.monotonic()
            self.queue.task_done()

    async def _publish(self) -> None:
        while not self.stop_event.is_set():
            now, mono = time.time(), time.monotonic()
            snapshot = self.engine.snapshot(
                now, mono, notifications_configured=self.notification_configured
            )
            self.last_assess_mono = time.monotonic()
            snapshot.health.last_persisted_at = self.store.health()["last_persisted_at"]
            snapshot.context["storage"] = self.store.health()
            snapshot.context["pipeline"] = {
                "queue_depth": self.queue.qsize(),
                "queue_capacity": self.queue.maxsize,
                "consumer_lag_seconds": mono - self.last_consume_mono,
            }
            self.persist_started_mono = time.monotonic()
            await self.store.persist(
                snapshot.model_dump(mode="json"), self.engine.drain_notifications()
            )
            self.persist_started_mono = None
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=1)

    def _progress_failure(self) -> str | None:
        now = time.monotonic()
        if self.queue.qsize() and now - self.last_consume_mono > self.config.watchdog_seconds:
            return "consumer_progress_stalled"
        if now - self.last_assess_mono > self.config.watchdog_seconds:
            return "assessment_progress_stalled"
        if (
            self.persist_started_mono is not None
            and now - self.persist_started_mono > self.config.watchdog_seconds
        ):
            return "persistence_progress_stalled"
        if self.store.failure is not None:
            return "storage_failure"
        return None

    def _start_watchdog(self, loop: asyncio.AbstractEventLoop) -> None:
        # Independent of asyncio: even a blocked event loop must exit for Docker restart.
        def watch() -> None:
            while not self._watchdog_stop.wait(0.25):
                reason = self._progress_failure()
                if reason is not None:
                    self.fatal_reason = reason
                    loop.call_soon_threadsafe(self.stop_event.set)
                    if not self._watchdog_stop.wait(self.config.shutdown_seconds):
                        self._fatal_exit(1)
                    return

        self._watchdog_thread = threading.Thread(
            target=watch, name="btc-progress-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    async def run(self) -> None:
        await asyncio.wait_for(self.store.start(), timeout=self.config.shutdown_seconds)
        tasks: list[asyncio.Task[None]] = []
        stopping: asyncio.Task[bool] | None = None
        identity_validated = False
        try:
            self.last_assess_mono = self.last_consume_mono = time.monotonic()
            self._start_watchdog(asyncio.get_running_loop())
            checkpoint = await asyncio.wait_for(
                self.store.load_checkpoint(), timeout=self.config.shutdown_seconds
            )
            if checkpoint is not None:
                previous = MonitorSnapshot.model_validate(checkpoint)
                if previous.source != self.engine.source:
                    raise RuntimeError("state_source_mismatch_use_separate_directory")
                self.engine.restore(previous, time.time(), time.monotonic())
            else:
                path = self.config.state_dir / "latest.json"
                if path.exists() and path.stat().st_size <= 1024 * 1024:
                    try:
                        previous = MonitorSnapshot.model_validate_json(
                            await asyncio.to_thread(path.read_bytes)
                        )
                        if previous.source != self.engine.source:
                            raise RuntimeError("state_source_mismatch_use_separate_directory")
                        self.engine.restore(previous, time.time(), time.monotonic())
                    except (OSError, ValueError):
                        self.engine.error("checkpoint_unavailable")
            identity_validated = True
            tasks = [
                asyncio.create_task(self._consume(), name="monitor-consumer"),
                asyncio.create_task(self._publish(), name="monitor-publisher"),
                asyncio.create_task(
                    self.feed_runner(
                        self.config, self.emit, self.reset, self.stop_event, self.engine.error
                    ),
                    name="monitor-feed",
                ),
            ]
            if self.notification_configured:
                sender = AsyncTelegram(
                    self.store,
                    os.environ["QT_TELEGRAM_BOT_TOKEN"],
                    os.environ["QT_TELEGRAM_CHAT_ID"],
                )
                tasks.append(asyncio.create_task(sender.run(), name="monitor-notifications"))
            stopping = asyncio.create_task(self.stop_event.wait())
            done, _ = await asyncio.wait([*tasks, stopping], return_when=asyncio.FIRST_COMPLETED)
            if self.fatal_reason:
                raise RuntimeError(self.fatal_reason)
            if stopping not in done:
                completed = next(task for task in tasks if task in done)
                if completed.cancelled():
                    raise RuntimeError("critical_task_cancelled")
                error = completed.exception()
                if error:
                    raise error
                raise RuntimeError(f"critical_task_stopped:{completed.get_name()}")
        except BaseException:
            self.fatal_reason = "internal_service_failure"
            raise
        finally:
            self.stop_event.set()
            for task in tasks:
                task.cancel()
            if stopping:
                stopping.cancel()
            cancelled = [*tasks, *([stopping] if stopping else [])]
            if cancelled:
                _, pending = await asyncio.wait(cancelled, timeout=self.config.shutdown_seconds / 3)
                if pending:
                    # Cancellation-resistant internal task: fail-stop rather than hang forever.
                    self._fatal_exit(1)
                    raise RuntimeError("task_shutdown_timeout")
                await asyncio.gather(*cancelled, return_exceptions=True)
            try:
                if identity_validated:
                    self.engine.reset(self.fatal_reason or "worker_stopped", time.time())
                    final = self.engine.snapshot(
                        time.time(),
                        time.monotonic(),
                        notifications_configured=self.notification_configured,
                    )
                    final.health.alive = False
                    final.health.status = "stopped"
                    final.health.reasons.append(self.fatal_reason or "worker_stopped")
                    await asyncio.wait_for(
                        self.store.persist(
                            final.model_dump(mode="json"), self.engine.drain_notifications()
                        ),
                        timeout=self.config.shutdown_seconds / 2,
                    )
            finally:
                try:
                    await self.store.stop(timeout=self.config.shutdown_seconds / 2)
                finally:
                    self._watchdog_stop.set()


async def run_monitor(config: MonitorConfig) -> None:
    service = MonitorService(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, service.stop_event.set)
    await service.run()


def read_status(state_dir: Path) -> MonitorSnapshot:
    path = state_dir / "latest.json"
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("snapshot_oversized")
    return MonitorSnapshot.model_validate_json(path.read_bytes())


def health_status(snapshot: MonitorSnapshot, now: float) -> bool:
    """Market outages are diagnostically unhealthy but do not mean a dead process."""
    return bool(
        snapshot.health.alive
        and -2 <= now - snapshot.published_at <= 10
        and snapshot.health.last_progress_at is not None
        and -2 <= now - snapshot.health.last_progress_at <= 10
        and snapshot.health.last_persisted_at is not None
        and -2 <= now - snapshot.health.last_persisted_at <= 10
    )


async def _self_test_in_dir(state_dir: Path) -> dict[str, object]:
    """Deterministic, network-free image smoke: engine, atomic storage, no execution."""
    config = MonitorConfig(state_dir=state_dir)
    engine = MonitorEngine(config)
    end = time.time()
    start = int(end) - 720
    for index in range(3):
        opened = start - 180 + index * 60
        closed = opened + 59.999
        engine.ingest(
            MarketEvent(
                channel="candle",
                event_time=closed,
                received_at=start,
                received_mono=1000,
                sequence=int(opened * 1000),
                price=100000,
                low=99000,
                high=101000,
                open_time=opened,
                close_time=closed,
                historical=True,
            ),
            start,
            1000,
        )
    for index in range(721):
        now, mono = start + index, 1000 + index
        price = 100000 + 1100 * math.sin(index * math.pi / 60)
        if index and index % 60 == 0:
            opened = now - 60
            engine.ingest(
                MarketEvent(
                    channel="candle",
                    event_time=now - 0.001,
                    received_at=now,
                    received_mono=mono,
                    sequence=int(opened * 1000),
                    price=price,
                    low=98000,
                    high=102000,
                    open_time=opened,
                    close_time=now - 0.001,
                ),
                now,
                mono,
            )
        engine.ingest(
            MarketEvent(
                channel="quote",
                received_at=now,
                received_mono=mono,
                sequence=index + 1,
                price=price,
                bid=price - 0.5,
                ask=price + 0.5,
            ),
            now,
            mono,
        )
        engine.ingest(
            MarketEvent(
                channel="trade",
                event_time=now,
                received_at=now,
                received_mono=mono,
                sequence=index + 1,
                price=price,
                quantity=1,
            ),
            now,
            mono,
        )
    snapshot = engine.snapshot(start + 720, 1720)
    store = SnapshotStore(state_dir)
    await store.start()
    try:
        await store.persist(snapshot.model_dump(mode="json"))
        loaded = read_status(state_dir)
        if loaded.assessment.state != "CANDIDATE" or loaded.live_execution_enabled:
            raise RuntimeError("offline_self_test_failed")
    finally:
        await store.stop()
    return {
        "ok": True,
        "mode": "offline",
        "assessment": snapshot.assessment.state,
        "live_execution_enabled": False,
    }


async def self_test() -> dict[str, object]:
    # Synthetic evidence never enters the configured production projection/outbox.
    with tempfile.TemporaryDirectory(prefix="qt-btc-self-test-") as directory:
        return await _self_test_in_dir(Path(directory))
