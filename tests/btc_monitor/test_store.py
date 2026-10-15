from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path

import pytest
import pytest_asyncio

from qt.btc_monitor.store import SnapshotStore, StoreError, timestamp


def healthy():
    return {
        "data_ready": True,
        "channels": {
            "trade": {"ready": True, "receive_age_seconds": 0, "event_age_seconds": 0},
            "quote": {
                "ready": True,
                "receive_age_seconds": 0,
                "event_age_seconds": None,
                "source_time_known": False,
            },
            "candle": {"ready": True, "event_age_seconds": 60},
        },
    }


def snapshot(revision=1, state="CONFIRMED", event_id="event-1", now=1000.0):
    return {
        "health": healthy(),
        "published_at": now,
        "recent_events": [
            {
                "event_id": event_id,
                "revision": revision,
                "state": state,
                "as_of": now,
                "expires_at": now + 100,
            }
        ],
    }


def notification(revision=1, event_id="event-1", expires_at=1100.0, kind="anomaly"):
    return {
        "event_id": event_id,
        "revision": revision,
        "notification_class": kind,
        "text": "Public BTC research signal",
        "expires_at": expires_at,
        "positive": True,
    }


def rows(path, table):
    with sqlite3.connect(path / "monitor.sqlite3") as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


@pytest_asyncio.fixture
async def store(tmp_path):
    instance = SnapshotStore(tmp_path, clock=lambda: 1000)
    await instance.start()
    yield instance
    with suppress(StoreError):
        await instance.stop()


@pytest.mark.asyncio
async def test_atomic_snapshot_and_transactional_dedup(store, tmp_path):
    data = snapshot()
    await store.persist(data, [notification(), notification(), notification(kind="other")])
    await store.persist(data, [notification()])
    assert json.loads((tmp_path / "latest.json").read_text()) == data
    assert len(rows(tmp_path, "decisions")) == 1
    assert len(rows(tmp_path, "outbox")) == 2
    assert await store.current_revision("event-1") == 1
    assert await store.current_revision("absent") is None
    assert len(await store.fetch_outbox()) == 2
    await store.flush()
    assert store.health()["last_persisted_at"] == 1000
    assert store.health()["writes"] == 2
    assert list(tmp_path.glob(".latest-*.tmp")) == []
    with sqlite3.connect(store.database_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_os_lock_rejects_duplicate_process_and_releases(store, tmp_path):
    code = """
import asyncio, sys
from qt.btc_monitor.store import SnapshotStore, StoreError
async def main():
    store = SnapshotStore(sys.argv[1])
    try:
        await store.start()
    except StoreError as exc:
        print(str(exc))
        return
    await store.stop()
    raise SystemExit(2)
asyncio.run(main())
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "storage_directory_in_use"
    await store.stop()
    replacement = SnapshotStore(tmp_path)
    await replacement.start()
    await replacement.stop()


@pytest.mark.asyncio
async def test_restart_retains_old_timestamp_and_dedup(store, tmp_path):
    await store.persist(snapshot(), [notification()])
    await store.stop()
    replacement = SnapshotStore(tmp_path, clock=lambda: 1005)
    await replacement.start()
    try:
        assert json.loads(replacement.latest_path.read_text())["published_at"] == 1000
        assert replacement.health()["last_persisted_at"] is None
        await replacement.persist(snapshot(), [notification()])
        assert len(rows(tmp_path, "outbox")) == 1
    finally:
        await replacement.stop()


@pytest.mark.asyncio
async def test_interrupted_export_keeps_last_good_json(store, tmp_path, monkeypatch):
    await store.persist(snapshot())
    original = store.latest_path.read_bytes()

    def interrupted(*args):
        raise OSError("fake disk failure")

    monkeypatch.setattr("qt.btc_monitor.store.os.replace", interrupted)
    with pytest.raises(StoreError, match="storage_write_failed"):
        await store.persist(snapshot(2))
    assert store.latest_path.read_bytes() == original
    assert list(tmp_path.glob(".latest-*.tmp")) == []
    assert store.failure is not None
    assert store.health()["ready"] is False
    with pytest.raises(StoreError):
        await store.flush()


@pytest.mark.asyncio
async def test_low_disk_fails_closed_preserving_projection(store, monkeypatch):
    await store.persist(snapshot())
    original = store.latest_path.read_bytes()

    def full():
        raise StoreError("storage_low_disk_space")

    monkeypatch.setattr(store, "_check_free_space", full)
    with pytest.raises(StoreError, match="storage_low_disk_space"):
        await store.persist(snapshot(2))
    assert store.latest_path.read_bytes() == original
    assert store.health()["error"] == "storage_low_disk_space"


@pytest.mark.asyncio
async def test_bounded_queue_is_fatal_and_disk_does_not_block_loop(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path, queue_size=1, clock=lambda: 1000)
    await store.start()
    entered, release = threading.Event(), threading.Event()
    original = store._atomic_export

    def slow(encoded):
        entered.set()
        assert release.wait(5)
        original(encoded)

    monkeypatch.setattr(store, "_atomic_export", slow)
    first = asyncio.create_task(store.persist(snapshot()))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    second = asyncio.create_task(store.persist(snapshot(2)))
    await asyncio.sleep(0.01)
    try:
        assert not first.done()  # A slow writer leaves the asyncio loop usable.
        assert store.health()["queue_depth"] == 1
        with pytest.raises(StoreError, match="storage_queue_full"):
            await store.persist(snapshot(3))
    finally:
        release.set()
    result = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(value, StoreError) for value in result)
    with pytest.raises(StoreError, match="storage_queue_full"):
        await store.stop()


@pytest.mark.asyncio
async def test_bounded_shutdown_retains_lock_until_thread_exits(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path, clock=lambda: 1000)
    await store.start()
    entered, release = threading.Event(), threading.Event()
    original = store._atomic_export

    def slow(encoded):
        entered.set()
        assert release.wait(5)
        original(encoded)

    monkeypatch.setattr(store, "_atomic_export", slow)
    writing = asyncio.create_task(store.persist(snapshot()))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    try:
        with pytest.raises(StoreError, match="storage_shutdown_timeout"):
            await store.stop(timeout=0.01)
        contender = SnapshotStore(tmp_path)
        with pytest.raises(StoreError, match="storage_directory_in_use"):
            await contender.start()
    finally:
        release.set()
    await writing
    await store.stop()


@pytest.mark.asyncio
async def test_outbox_capacity_rolls_back_revision_and_outbox(tmp_path):
    store = SnapshotStore(tmp_path, clock=lambda: 1000, max_outbox=1)
    await store.start()
    await store.persist(snapshot(), [notification()])
    with pytest.raises(StoreError, match="storage_outbox_capacity"):
        await store.persist(snapshot(2), [notification(2)])
    assert len(rows(tmp_path, "decisions")) == 1
    assert len(rows(tmp_path, "outbox")) == 1
    assert json.loads(store.latest_path.read_text())["recent_events"][0]["revision"] == 1
    with pytest.raises(StoreError):
        await store.stop()


@pytest.mark.asyncio
async def test_bounded_retention_reuses_terminal_slots(tmp_path):
    store = SnapshotStore(tmp_path, clock=lambda: 1000, max_decisions=3, max_outbox=2)
    await store.start()
    try:
        for revision in range(1, 8):
            await store.persist(snapshot(revision), [notification(revision)])
            await store.update_outbox("event-1", revision, "anomaly", status="accepted", attempts=1)
        assert len(rows(tmp_path, "decisions")) == 3
        assert len(rows(tmp_path, "outbox")) == 2
        assert await store.current_revision("event-1") == 7
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_expiry_and_withdrawal_suppress_queued_positive(store, tmp_path):
    await store.persist(snapshot(), [notification(), notification(kind="expired", expires_at=999)])
    await store.persist(snapshot(2, "INVALIDATED"))
    assert await store.fetch_outbox() == []
    assert {row["status"] for row in rows(tmp_path, "outbox")} == {"obsolete", "expired"}


@pytest.mark.asyncio
async def test_new_assessment_identity_supersedes_grid_positive(store):
    assessment = {
        "assessment_id": "grid-1",
        "revision": 1,
        "state": "CANDIDATE",
        "expires_at": 1100,
    }
    await store.persist(
        {"published_at": 1000, "health": healthy(), "assessment": assessment},
        [notification(event_id="grid-1")],
    )
    assert len(await store.fetch_outbox()) == 1
    await store.persist(
        {
            "health": healthy(),
            "assessment": dict(assessment, assessment_id="grid-2", state="UNSUITABLE"),
        }
    )
    assert await store.fetch_outbox() == []


@pytest.mark.asyncio
async def test_interrupted_sending_is_unknown_after_restart(store, tmp_path):
    await store.persist(snapshot(), [notification()])
    await store.update_outbox("event-1", 1, "anomaly", status="sending", attempts=1)
    await store.stop()
    restarted = SnapshotStore(tmp_path, clock=lambda: 1001)
    await restarted.start()
    try:
        assert await restarted.fetch_outbox() == []
        assert rows(tmp_path, "outbox")[0]["status"] == "unknown"
        assert rows(tmp_path, "outbox")[0]["error_code"] == "interrupted_send"
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_revisions_are_immutable(store):
    await store.persist(snapshot())
    with pytest.raises(StoreError, match="storage_revision_conflict"):
        await store.persist(snapshot(state="INVALIDATED"))


@pytest.mark.asyncio
async def test_nonfinite_or_oversized_payload_is_fatal(tmp_path):
    for data in ({"price": float("nan")}, {"text": "x" * 101}):
        state = tmp_path / str(len(data.get("text", "")))
        store = SnapshotStore(state, max_snapshot_bytes=100)
        await store.start()
        with pytest.raises(StoreError):
            await store.persist(data)
        assert not store.latest_path.exists()
        with pytest.raises(StoreError):
            await store.stop()


def test_timestamp_rejects_naive_and_nonfinite():
    assert timestamp("2026-10-15T00:00:00Z") > 0
    with pytest.raises(ValueError, match="timezone"):
        timestamp("2026-10-15T00:00:00")
    with pytest.raises(ValueError, match="finite"):
        timestamp(float("inf"))


@pytest.mark.asyncio
async def test_full_sqlite_disk_rolls_back_without_replacing_last_projection(
    store, tmp_path, monkeypatch
):
    await store.persist(snapshot())
    before = store.latest_path.read_bytes()
    original = store._persist

    def full(snapshot_value, notifications):
        # Exercise SQLite's actual SQLITE_FULL/rollback path without filling the host disk.
        page_count = store._db.execute("PRAGMA page_count").fetchone()[0]
        store._db.execute(f"PRAGMA max_page_count={page_count}")
        return original(snapshot_value, notifications)

    monkeypatch.setattr(store, "_persist", full)
    with pytest.raises(StoreError, match="storage_write_failed"):
        await store.persist(dict(snapshot(2), context={"large": "x" * 100000}), [notification(2)])
    assert store.latest_path.read_bytes() == before
    assert len(rows(tmp_path, "decisions")) == 1
    assert len(rows(tmp_path, "outbox")) == 0


@pytest.mark.asyncio
async def test_projection_database_and_lock_are_group_readable(store, tmp_path):
    await store.persist(snapshot())
    for path in (store.latest_path, store.database_path, tmp_path / ".writer.lock"):
        assert path.stat().st_mode & 0o777 == 0o640


@pytest.mark.asyncio
async def test_continuous_candidate_revisions_keep_notice_current(store):
    assessment = {"assessment_id": "grid", "revision": 1, "state": "CANDIDATE", "expires_at": 1100}
    await store.persist(
        {"published_at": 1000, "health": healthy(), "assessment": assessment},
        [notification(event_id="grid")],
    )
    await store.persist(
        {"published_at": 1000, "health": healthy(), "assessment": dict(assessment, revision=2)}
    )
    items = await store.fetch_outbox()
    assert len(items) == 1
    assert await store.notification_is_current(items[0])


@pytest.mark.asyncio
async def test_withdrawal_barrier_survives_retention_and_restart(store, tmp_path):
    assessment = {"assessment_id": "grid", "revision": 1, "state": "CANDIDATE", "expires_at": 1100}
    await store.persist(
        {"published_at": 1000, "health": healthy(), "assessment": assessment},
        [notification(event_id="grid")],
    )
    await store.persist(
        {
            "health": healthy(),
            "assessment": dict(assessment, revision=2, state="UNSUITABLE"),
        }
    )
    await store.persist(
        {"published_at": 1000, "health": healthy(), "assessment": dict(assessment, revision=3)}
    )
    await store.stop()
    restarted = SnapshotStore(tmp_path, clock=lambda: 1000, max_decisions=1)
    await restarted.start()
    try:
        await restarted.persist(
            {"published_at": 1000, "health": healthy(), "assessment": dict(assessment, revision=4)}
        )
        assert len(rows(tmp_path, "decisions")) == 1
        assert await restarted.fetch_outbox() == []
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_claim_is_atomic_and_only_once(store):
    await store.persist(snapshot(), [notification()])
    first, second = await store.fetch_outbox(), await store.fetch_outbox()
    assert await store.claim_outbox(first[0])
    assert not await store.claim_outbox(second[0])


@pytest.mark.asyncio
async def test_retention_prunes_old_decisions_and_terminal_outbox(tmp_path):
    now = [1000.0]
    store = SnapshotStore(tmp_path, clock=lambda: now[0], retention_days=1 / 86400)
    await store.start()
    try:
        await store.persist(snapshot(), [notification()])
        await store.update_outbox("event-1", 1, "anomaly", status="accepted", attempts=1)
        now[0] = 1002
        await store.persist(snapshot(2))
        assert len(rows(tmp_path, "decisions")) == 1
        assert rows(tmp_path, "outbox") == []
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_positive_fails_closed_for_unready_or_missing_health(store):
    for health in ({"data_ready": False}, {}, {"data_ready": "true"}):
        data = snapshot()
        data["health"] = health
        await store.persist(data, [notification()])
        assert await store.fetch_outbox() == []


@pytest.mark.asyncio
async def test_restart_does_not_trust_historical_positive_readiness(store, tmp_path):
    await store.persist(snapshot(), [notification()])
    await store.stop()
    restarted = SnapshotStore(tmp_path, clock=lambda: 1001)
    await restarted.start()
    try:
        assert await restarted.fetch_outbox() == []
        assert rows(tmp_path, "outbox")[0]["status"] == "obsolete"
    finally:
        await restarted.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["trade_receive", "trade_source", "quote_receive", "candle_source", "projection"]
)
async def test_positive_delivery_rechecks_advanced_channel_and_projection_age(tmp_path, field):
    now = [1000.0]
    store = SnapshotStore(tmp_path, clock=lambda: now[0])
    await store.start()
    data = snapshot()
    if field == "trade_receive":
        data["health"]["channels"]["trade"]["receive_age_seconds"] = 4.5
    elif field == "trade_source":
        data["health"]["channels"]["trade"]["event_age_seconds"] = 4.5
    elif field == "quote_receive":
        data["health"]["channels"]["quote"]["receive_age_seconds"] = 4.5
    elif field == "candle_source":
        data["health"]["channels"]["candle"]["event_age_seconds"] = 124.5
    try:
        await store.persist(data, [notification()])
        item = (await store.fetch_outbox())[0]
        now[0] += 6 if field == "projection" else 1
        assert not await store.notification_is_current(item)
        assert not await store.claim_outbox(item)
        assert await store.fetch_outbox() == []
    finally:
        await store.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken", ["missing_channel", "future_projection", "negative_receive", "unknown_trade_time"]
)
async def test_positive_fails_closed_for_malformed_or_future_source_freshness(store, broken):
    data = snapshot()
    if broken == "missing_channel":
        del data["health"]["channels"]["quote"]
    elif broken == "future_projection":
        data["published_at"] = 1003
    elif broken == "negative_receive":
        data["health"]["channels"]["trade"]["receive_age_seconds"] = -1
    else:
        data["health"]["channels"]["trade"]["event_age_seconds"] = None
    await store.persist(data, [notification()])
    assert await store.fetch_outbox() == []


@pytest.mark.asyncio
async def test_nonpositive_withdrawal_is_deliverable_with_unhealthy_source(store):
    data = snapshot(2, "INVALIDATED")
    data["health"] = {"data_ready": False}
    item = dict(notification(2), positive=False)
    await store.persist(data, [item])
    assert len(await store.fetch_outbox()) == 1


@pytest.mark.asyncio
async def test_submission_racing_worker_failure_never_orphans_future(store, monkeypatch):
    original = store._queue.put_nowait

    def failing_put(item):
        # Simulate the writer failing and finishing its drain just before enqueue.
        store._failure = StoreError("storage_write_failed")
        original(item)

    monkeypatch.setattr(store._queue, "put_nowait", failing_put)
    with pytest.raises(StoreError, match="storage_write_failed"):
        await asyncio.wait_for(store.flush(), timeout=1)


@pytest.mark.asyncio
async def test_checkpoint_is_empty_initially_and_returns_detached_state(store):
    assert await store.load_checkpoint() is None
    await store.persist(snapshot())
    restored = await store.load_checkpoint()
    assert restored == snapshot()
    restored["recent_events"][0]["revision"] = 100
    assert (await store.load_checkpoint())["recent_events"][0]["revision"] == 1


@pytest.mark.asyncio
async def test_restart_recovers_sqlite_checkpoint_after_committed_export_interruption(
    store, tmp_path, monkeypatch
):
    await store.persist(snapshot())

    def interrupted(encoded):
        raise OSError("interrupted after SQLite commit, before atomic JSON export")

    monkeypatch.setattr(store, "_atomic_export", interrupted)
    with pytest.raises(StoreError, match="storage_write_failed"):
        await store.persist(snapshot(2), [notification(2)])
    with pytest.raises(StoreError):
        await store.stop()
    assert json.loads(store.latest_path.read_text())["recent_events"][0]["revision"] == 1
    restarted = SnapshotStore(tmp_path, clock=lambda: 1000)
    await restarted.start()
    try:
        checkpoint = await restarted.load_checkpoint()
        assert checkpoint["recent_events"][0]["revision"] == 2
        assert await restarted.current_revision("event-1") == 2
        assert not await restarted.notification_is_current(notification(2))
        # Restoring the durable identity produces revision 3, avoiding a conflict
        # with revision 2 already committed before the interrupted export.
        await restarted.persist(snapshot(checkpoint["recent_events"][0]["revision"] + 1))
        assert json.loads(restarted.latest_path.read_text())["recent_events"][0]["revision"] == 3
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_checkpoint_size_is_bounded_before_decoding_on_start(tmp_path):
    first = SnapshotStore(tmp_path, max_snapshot_bytes=1024)
    await first.start()
    await first.persist({"large": "x" * 200})
    await first.stop()
    restarted = SnapshotStore(tmp_path, max_snapshot_bytes=100)
    with pytest.raises(StoreError, match="storage_checkpoint_too_large"):
        await restarted.start()
    with pytest.raises(StoreError):
        await restarted.stop()
