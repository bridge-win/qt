from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from qt.btc_monitor.config import MonitorConfig
from qt.btc_monitor.models import MarketEvent
from qt.btc_monitor.service import MonitorService, health_status, read_status, self_test
from qt.btc_monitor.store import SnapshotStore


async def outage_feed(config, emit, reset, stop, report_error):
    reset("simulated_outage")
    await stop.wait()


@pytest.mark.asyncio
async def test_exchange_outage_stays_live_and_stop_is_clean(tmp_path):
    service = MonitorService(MonitorConfig(state_dir=tmp_path), feed_runner=outage_feed)
    task = asyncio.create_task(service.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if service.store.health()["writes"] >= 2:
            break
    snapshot = read_status(tmp_path)
    assert health_status(snapshot, time.time())
    assert not snapshot.health.data_ready
    assert snapshot.assessment.state == "INSUFFICIENT_DATA"
    service.stop_event.set()
    await asyncio.wait_for(task, timeout=2)
    assert not read_status(tmp_path).health.alive
    assert not service.store.health()["worker_alive"]


@pytest.mark.asyncio
async def test_unexpected_critical_task_completion_is_fatal(tmp_path):
    async def stopped_feed(*args):
        return None

    service = MonitorService(MonitorConfig(state_dir=tmp_path), feed_runner=stopped_feed)
    with pytest.raises(RuntimeError, match="critical_task_stopped"):
        await service.run()
    assert not read_status(tmp_path).health.alive
    assert service.fatal_reason


def test_queue_overflow_never_silently_loses_path(tmp_path):
    service = MonitorService(MonitorConfig(state_dir=tmp_path, queue_size=8))
    now, mono = time.time(), time.monotonic()
    for sequence in range(9):
        service.emit(
            MarketEvent(
                channel="trade",
                received_at=now,
                received_mono=mono,
                event_time=now,
                sequence=sequence,
                price=100000,
            )
        )
    assert service.queue.empty()
    assert service.engine.errors["ingest_queue_overflow"] == 1
    assert service.engine.gap_reason == "ingest_queue_overflow"
    assert service.engine.coverage_start is None


@pytest.mark.asyncio
async def test_slow_storage_does_not_block_ingest_or_event_loop(tmp_path):
    store = SnapshotStore(tmp_path)
    original = store._atomic_export
    entered = asyncio.Event()
    release_export = threading.Event()

    def slow_export(*args):
        # Keep storage blocked until ingestion proves progress. A fixed sleep
        # races CPU scheduling under container/test load and can finish first
        # even when the event loop is correctly independent of the writer.
        if not release_export.wait(timeout=5):
            raise TimeoutError("test did not release the blocked writer")
        return original(*args)

    store._atomic_export = slow_export

    async def feed(config, emit, reset, stop, report_error):
        for sequence in range(1, 51):
            now, mono = time.time(), time.monotonic()
            emit(
                MarketEvent(
                    channel="trade",
                    received_at=now,
                    received_mono=mono,
                    event_time=now,
                    sequence=sequence,
                    price=100000,
                )
            )
            await asyncio.sleep(0.002)
        entered.set()
        await stop.wait()

    service = MonitorService(MonitorConfig(state_dir=tmp_path), feed_runner=feed, store=store)
    task = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert (
            service.engine.micro.buckets[-1].count
            + sum(x.count for x in list(service.engine.micro.buckets)[:-1])
            == 50
        )
        assert store.health()["writes"] == 0
    finally:
        release_export.set()
        service.stop_event.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_fatal_storage_exits_and_releases_writer_lock(tmp_path):
    store = SnapshotStore(tmp_path)

    def disk_full(*args):
        raise OSError("simulated_full_disk")

    store._atomic_export = disk_full
    service = MonitorService(
        MonitorConfig(state_dir=tmp_path), feed_runner=outage_feed, store=store
    )
    with pytest.raises(Exception, match="storage"):
        await service.run()
    assert service.fatal_reason
    replacement = SnapshotStore(tmp_path)
    await replacement.start()
    await replacement.stop()


@pytest.mark.asyncio
async def test_selftest_never_touches_production_state(tmp_path, monkeypatch):
    sentinel = tmp_path / "latest.json"
    sentinel.write_text("production-sentinel")
    monkeypatch.setenv("QT_BTC_MONITOR_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QT_BTC_MONITOR_CONFIG", str(tmp_path / "nonexistent-secret-config"))
    assert (await self_test())["ok"]
    assert sentinel.read_text() == "production-sentinel"
    assert list(tmp_path.iterdir()) == [sentinel]
    result = subprocess.run(
        [sys.executable, "-m", "qt.btc_monitor", "self-test"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["ok"]
    assert sentinel.read_text() == "production-sentinel"


def test_no_execution_imports_even_if_existing_qt_env_is_live(tmp_path):
    source = textwrap.dedent("""
        import sys
        import qt.btc_monitor.__main__
        assert not any(name.startswith(("qt.btc_agent", "qt.execution", "ccxt")) for name in sys.modules)
        from qt.btc_monitor.models import DecisionRecord
        from pydantic import ValidationError
        try:
            DecisionRecord(assessment_id="x", as_of=1, expires_at=2, state="CANDIDATE", reasons=[], live_execution_enabled=True)
        except ValidationError:
            pass
        else:
            raise AssertionError("execution flag mutable")
    """)
    result = subprocess.run(
        [sys.executable, "-c", source],
        env={**os.environ, "QT_ENV": "live"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_independent_watchdog_exits_fully_blocked_eventloop(tmp_path):
    source = textwrap.dedent("""
        import asyncio, sys, time
        from pathlib import Path
        from qt.btc_monitor.config import MonitorConfig
        from qt.btc_monitor.service import MonitorService
        async def blocked_feed(*args):
            await asyncio.sleep(.05)
            time.sleep(30)
        async def main():
            service = MonitorService(MonitorConfig(state_dir=Path(sys.argv[1]), watchdog_seconds=3, shutdown_seconds=1), feed_runner=blocked_feed)
            await service.run()
        asyncio.run(main())
    """)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path)], capture_output=True, text=True, timeout=8
    )
    assert result.returncode == 1
    assert time.monotonic() - started < 7


def test_cancellation_resistant_task_has_bounded_shutdown(tmp_path):
    source = textwrap.dedent("""
        import asyncio, sys
        from pathlib import Path
        from qt.btc_monitor.config import MonitorConfig
        from qt.btc_monitor.service import MonitorService
        async def resistant_feed(*args):
            while True:
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    pass
        async def main():
            service = MonitorService(MonitorConfig(state_dir=Path(sys.argv[1]), shutdown_seconds=1), feed_runner=resistant_feed)
            asyncio.get_running_loop().call_later(.1, service.stop_event.set)
            await service.run()
        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path)], capture_output=True, text=True, timeout=4
    )
    assert result.returncode == 1


def test_watchdog_requires_consumer_progress_with_backlog(tmp_path):
    service = MonitorService(MonitorConfig(state_dir=tmp_path, watchdog_seconds=3))
    now, mono = time.time(), time.monotonic()
    service.emit(
        MarketEvent(
            channel="trade",
            event_time=now,
            received_at=now,
            received_mono=mono,
            sequence=1,
            price=100000,
        )
    )
    service.last_consume_mono = mono - 4
    service.last_assess_mono = mono
    assert service._progress_failure() == "consumer_progress_stalled"
    service.queue.get_nowait()
    assert service._progress_failure() is None


@pytest.mark.asyncio
async def test_store_start_is_bounded_before_processing_begins(tmp_path):
    class SlowStart:
        async def start(self):
            await asyncio.sleep(30)

    service = MonitorService(
        MonitorConfig(state_dir=tmp_path, shutdown_seconds=1), store=SlowStart()
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(service.run(), timeout=2)
    assert not service.engine.micro.buckets


@pytest.mark.parametrize("venue,environment", [("binance", "testnet"), ("okx", "demo")])
@pytest.mark.asyncio
async def test_slow_socket_close_preserves_clean_shutdown(tmp_path, monkeypatch, venue, environment):
    from qt.btc_monitor import feed

    connected = asyncio.Event()

    class PublicClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return self

        def raise_for_status(self):
            return None

        def json(self):
            return [] if venue == "binance" else {"code": "0", "data": []}

    class SlowCloseSocket:
        def __init__(self, close_timeout):
            self.close_timeout = close_timeout

        async def __aenter__(self):
            connected.set()
            return self

        async def __aexit__(self, *args):
            # A peer that never acknowledges close consumes the transport budget.
            await asyncio.sleep(self.close_timeout)

        async def send(self, message):
            return None

        async def recv(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(feed.httpx, "AsyncClient", lambda **kwargs: PublicClient())
    monkeypatch.setattr(
        feed.websockets, "connect",
        lambda *args, **kwargs: SlowCloseSocket(kwargs["close_timeout"]),
    )
    forced_exits = []
    service = MonitorService(
        MonitorConfig(state_dir=tmp_path, venue=venue, environment=environment, shutdown_seconds=3),
        fatal_exit=forced_exits.append,
    )
    task = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(connected.wait(), timeout=2)
        service.stop_event.set()
        await asyncio.wait_for(task, timeout=4)
        assert not forced_exits
        assert not read_status(tmp_path).health.alive
        assert not service.store.health()["worker_alive"]
    finally:
        # Also release storage on the failing pre-fix path.
        service.stop_event.set()
        await service.store.stop(timeout=1)
