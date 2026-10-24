"""Public-feed fixtures exercise bootstrap, gaps, bounded buffering and replay."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager

import pytest

from qt.datasets import DatasetRegistry
from qt.marketdata.collector import (
    BinanceCollector,
    CollectorCallbackError,
    CollectorPersistenceError,
    normalize_binance_message,
)

_NOW = 1_700_000_000_000_000_000


def depth(first=101, last=101):
    return {"e": "depthUpdate", "E": _NOW // 1_000_000, "s": "BTCUSDT", "U": first,
            "u": last, "b": [["100", "2"]], "a": [["102", "2"]]}


def trade():
    return {"e": "trade", "E": _NOW // 1_000_000, "s": "BTCUSDT", "t": 1,
            "p": "101", "q": "0.01", "T": _NOW // 1_000_000, "m": False}


def snapshot(sequence=100):
    return {"lastUpdateId": sequence, "bids": [["100", "1"]], "asks": [["102", "1"]]}


def clock():
    current = [_NOW + 1]

    def now():
        current[0] += 10_000
        return current[0]

    return now


@pytest.mark.asyncio
async def test_bootstrap_streams_and_parquet_envelopes_replay_in_receipt_order(tmp_path):
    registry = DatasetRegistry(tmp_path)
    stop = asyncio.Event()
    emitted, manifests = [], []

    async def receive(event, book):
        emitted.append((event, book))
        if event.event_type == "trade":
            stop.set()

    @asynccontextmanager
    async def stream():
        async def frames():
            for value in (depth(), depth(102, 102), trade()):
                yield json.dumps(value)
                await asyncio.sleep(.01)
            await stop.wait()

        yield frames()

    async def fetch():
        return snapshot()

    collector = BinanceCollector(registry, clock=clock(), on_event=receive,
                                  on_manifest=manifests.append, batch_size=2, maximum_buffer=20)
    await collector.run(stop=stop, maximum_sessions=1, stream_factory=stream, snapshot_fetcher=fetch)
    assert [item[0].event_type for item in emitted] == ["delta", "delta", "trade"]
    assert all(item[1] is not None for item in emitted)
    replay = [event for manifest in manifests for event in registry.events(manifest["dataset_id"])]
    assert [item.event_type for item in replay] == ["delta", "snapshot", "delta", "trade"]
    assert [item.received_ts_ns for item in replay] == sorted(item.received_ts_ns for item in replay)
    assert replay[1].exchange_ts_ns == 0 and "EXCHANGE_TIME_UNKNOWN" in replay[1].quality_flags


@pytest.mark.asyncio
async def test_gap_never_reaches_decision_callback_and_requires_new_connection(tmp_path):
    registry = DatasetRegistry(tmp_path)
    health, emitted = [], []

    async def receive(event, book):
        emitted.append(event)

    @asynccontextmanager
    async def stream():
        async def frames():
            for value in (depth(), depth(103, 103)):
                yield json.dumps(value)
                await asyncio.sleep(.01)
            await asyncio.Event().wait()

        yield frames()

    async def fetch():
        return snapshot()

    collector = BinanceCollector(registry, clock=clock(), on_event=receive, on_health=health.append)
    await collector.run(maximum_sessions=1, stream_factory=stream, snapshot_fetcher=fetch)
    assert [event.sequence for event in emitted] == [101]
    assert not collector.book.is_valid
    assert any(item["status"] == "reconnecting" for item in health)
    assert list((registry.root / "manifests").glob("*.json"))


@pytest.mark.asyncio
async def test_overflow_invalidates_without_processing_truncated_buffer(tmp_path):
    health, emitted = [], []

    async def receive(event, book):
        emitted.append(event)

    @asynccontextmanager
    async def stream():
        async def frames():
            for value in range(100):
                yield json.dumps(depth(101 + value, 101 + value))

        yield frames()

    collector = BinanceCollector(DatasetRegistry(tmp_path), clock=clock(), batch_size=1,
                                  maximum_buffer=2, on_event=receive, on_health=health.append)
    await collector.run(maximum_sessions=1, stream_factory=stream,
                        snapshot_fetcher=lambda: asyncio.sleep(0, result=snapshot()))
    assert not emitted and not collector.book.is_valid
    assert any(item["status"] == "reconnecting" for item in health)


def test_normalizer_rejects_wrong_symbols_numbers_and_forming_bars():
    item = normalize_binance_message(trade(), symbol="BTCUSDT", received_ts_ns=_NOW + 1)
    assert item.payload["aggressor_side"] == "buy"
    assert item.payload["quantity"] == "0.01"
    with pytest.raises(ValueError):
        normalize_binance_message({**trade(), "p": 101.0}, symbol="BTCUSDT", received_ts_ns=_NOW + 1)
    with pytest.raises(RuntimeError):
        normalize_binance_message({**trade(), "s": "ETHUSDT"}, symbol="BTCUSDT", received_ts_ns=_NOW + 1)
    forming = {"e": "kline", "E": _NOW // 1_000_000, "s": "BTCUSDT", "k": {"x": False}}
    assert normalize_binance_message(forming, symbol="BTCUSDT", received_ts_ns=_NOW + 1) is None


def test_closed_kline_uses_exclusive_close_and_causal_availability():
    close_ms = _NOW // 1_000_000 - 1
    raw = {"e": "kline", "E": close_ms + 1, "s": "BTCUSDT", "k": {
        "x": True, "t": close_ms + 1 - 60_000, "T": close_ms, "o": "100", "h": "102",
        "l": "99", "c": "101", "v": "3", "V": "2", "i": "1m",
    }}
    event = normalize_binance_message(raw, symbol="BTCUSDT", received_ts_ns=_NOW + 1)
    assert event.exchange_ts_ns == _NOW
    assert event.payload["buy_volume"] == "2" and event.payload["sell_volume"] == "1"
    assert event.payload["close_ts_ns"] == _NOW


def test_kline_interval_identity_and_decimal_difference_are_exact():
    close_ms = _NOW // 1_000_000 - 1
    bar = {"x": True, "t": close_ms + 1 - 60_000, "T": close_ms, "o": "100", "h": "102",
           "l": "99", "c": "101", "v": "12345678901234567890.123456789012345678",
           "V": "0.000000000000000001", "i": "1m"}
    raw = {"e": "kline", "E": close_ms + 1, "s": "BTCUSDT", "k": bar}
    event = normalize_binance_message(raw, symbol="BTCUSDT", received_ts_ns=_NOW + 1)
    assert event.payload["sell_volume"] == "12345678901234567890.123456789012345677"
    for change in ({"v": 1.5}, {"V": float("nan")}, {"T": close_ms + 1}, {"s": "ETHUSDT"}):
        with pytest.raises((ValueError, RuntimeError)):
            normalize_binance_message({**raw, "k": {**bar, **change}}, symbol="BTCUSDT",
                                      received_ts_ns=_NOW + 1)


@pytest.mark.asyncio
async def test_reconnect_resnapshots_and_never_continues_the_old_sequence(tmp_path):
    stop = asyncio.Event()
    connections, emitted, sleeps = [0], [], []

    async def receive(event, view):
        emitted.append(event)
        if event.event_type == "trade":
            stop.set()

    @asynccontextmanager
    async def stream():
        connections[0] += 1
        frames_for_session = ((depth(), depth(103, 103)) if connections[0] == 1
                              else (depth(201, 201), trade()))

        async def frames():
            for value in frames_for_session:
                yield json.dumps(value)
                await asyncio.sleep(.01)
            await stop.wait()

        yield frames()

    async def fetch():
        return snapshot(100 if connections[0] == 1 else 200)

    async def backoff(seconds):
        sleeps.append(seconds)

    collector = BinanceCollector(DatasetRegistry(tmp_path), clock=clock(), on_event=receive)
    await collector.run(stop=stop, maximum_sessions=2, stream_factory=stream,
                        snapshot_fetcher=fetch, sleep=backoff)
    assert [item.sequence for item in emitted] == [101, 201, 1]
    assert connections[0] == 2 and sleeps == [1.0]


@pytest.mark.asyncio
async def test_initial_websocket_handshake_failure_reconnects_then_bootstraps(tmp_path):
    from websockets.exceptions import InvalidHandshake

    connections, sleeps, emitted = [], [], []
    stop = asyncio.Event()

    @asynccontextmanager
    async def stream():
        connections.append(1)
        if len(connections) == 1:
            raise InvalidHandshake("fixture rejected upgrade")

        async def frames():
            yield json.dumps(depth())
            await asyncio.Event().wait()

        yield frames()

    async def receive(event, book):
        emitted.append(event)
        assert book is not None
        stop.set()

    async def backoff(seconds):
        sleeps.append(seconds)

    collector = BinanceCollector(DatasetRegistry(tmp_path), clock=clock(), on_event=receive)
    await collector.run(stop=stop, maximum_sessions=2, stream_factory=stream,
                        snapshot_fetcher=lambda: asyncio.sleep(0, result=snapshot()), sleep=backoff)
    assert len(connections) == 2 and sleeps == [1.0]
    assert [event.sequence for event in emitted] == [101]


@pytest.mark.parametrize("message", [None, [], "not an object", {**trade(), "p": "NaN"},
    {**trade(), "q": None}, {**trade(), "t": 2**63},
    {"e": "kline", "E": _NOW // 1_000_000, "s": "BTCUSDT", "k": {"x": True}},
    {**depth(), "a": [["102", float("nan")]]},
])
@pytest.mark.asyncio
async def test_malformed_frames_resync_without_reaching_callback(tmp_path, message):
    emitted, health = [], []

    @asynccontextmanager
    async def stream():
        async def frames():
            yield json.dumps(message)
            await asyncio.Event().wait()

        yield frames()

    async def receive(event, book):
        emitted.append(event)

    collector = BinanceCollector(DatasetRegistry(tmp_path), clock=clock(), on_event=receive,
                                  on_health=health.append)
    await collector.run(maximum_sessions=1, stream_factory=stream,
                        snapshot_fetcher=lambda: asyncio.sleep(0, result=snapshot()))
    assert not emitted and not collector.book.is_valid
    assert any(item["status"] == "reconnecting" for item in health)


@pytest.mark.asyncio
async def test_quiet_initial_socket_times_out_and_stop_interrupts_handshake(tmp_path):
    health = []

    @asynccontextmanager
    async def quiet_stream():
        async def frames():
            await asyncio.Event().wait()
            yield "unreachable"

        yield frames()

    collector = BinanceCollector(DatasetRegistry(tmp_path), idle_timeout_seconds=.01,
                                  on_health=health.append)
    await asyncio.wait_for(collector.run(maximum_sessions=1, stream_factory=quiet_stream), timeout=1)
    assert any(item["status"] == "reconnecting" for item in health)

    entered, canceled, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def hanging_handshake():
        entered.set()
        try:
            await asyncio.Event().wait()
            yield None
        finally:
            canceled.set()

    task = asyncio.create_task(collector.run(stop=stop, stream_factory=hanging_handshake))
    await entered.wait()
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert canceled.is_set()


@pytest.mark.asyncio
async def test_trade_only_stream_cannot_hold_bootstrap_open_forever(tmp_path):
    health = []

    @asynccontextmanager
    async def stream():
        async def frames():
            while True:
                yield json.dumps(trade())
                await asyncio.sleep(.002)

        yield frames()

    collector = BinanceCollector(DatasetRegistry(tmp_path), clock=clock(), on_health=health.append,
                                  bootstrap_timeout_seconds=.02)
    await asyncio.wait_for(collector.run(maximum_sessions=1, stream_factory=stream), timeout=1)
    assert any(item["status"] == "reconnecting" for item in health)
    assert not collector.book.is_valid


@pytest.mark.asyncio
async def test_byte_backpressure_is_independent_of_frame_count(tmp_path):
    emitted = []

    @asynccontextmanager
    async def stream():
        async def frames():
            for _ in range(10):
                yield json.dumps({**depth(), "padding": "x" * 400})

        yield frames()

    async def receive(event, book):
        emitted.append(event)

    collector = BinanceCollector(DatasetRegistry(tmp_path), clock=clock(), maximum_buffer_bytes=1024,
                                  maximum_buffer=100, batch_size=1, on_event=receive)
    await collector.run(maximum_sessions=1, stream_factory=stream)
    assert not emitted and not collector.book.is_valid


@pytest.mark.asyncio
async def test_consumer_failure_is_fatal_instead_of_a_network_replay(tmp_path):
    connections = []

    @asynccontextmanager
    async def stream():
        connections.append(1)

        async def frames():
            yield json.dumps(trade())
            await asyncio.Event().wait()

        yield frames()

    async def receive(event, book):
        raise ValueError("downstream invariant failed")

    collector = BinanceCollector(DatasetRegistry(tmp_path), clock=clock(), on_event=receive)
    with pytest.raises(CollectorCallbackError, match="event_consumer_failed"):
        await collector.run(maximum_sessions=2, stream_factory=stream)
    assert len(connections) == 1 and not collector.book.is_valid


@pytest.mark.asyncio
async def test_concurrent_flush_publishes_each_record_once(tmp_path, monkeypatch):
    registry, manifests = DatasetRegistry(tmp_path), []
    collector = BinanceCollector(registry, clock=clock(), on_manifest=manifests.append)
    await collector._record(normalize_binance_message(trade(), symbol="BTCUSDT", received_ts_ns=_NOW + 1))
    original, calls = registry.register_table, []

    def register(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "register_table", register)
    await asyncio.gather(*(collector.flush() for _ in range(10)))
    assert len(calls) == len(manifests) == 1
    assert len(list(registry.events(manifests[0]["dataset_id"]))) == 1
    assert collector._record_bytes == 0


@pytest.mark.asyncio
async def test_failed_manifest_notification_retains_same_identity_and_new_records(tmp_path):
    registry, notifications = DatasetRegistry(tmp_path), []

    def publish(manifest):
        notifications.append(manifest["dataset_id"])
        if len(notifications) == 1:
            raise OSError("temporary catalog unavailable")

    collector = BinanceCollector(registry, clock=clock(), on_manifest=publish)
    await collector._record(normalize_binance_message(trade(), symbol="BTCUSDT", received_ts_ns=_NOW + 1))
    with pytest.raises(CollectorPersistenceError, match="manifest_notification_failed"):
        await collector.flush()
    assert len(collector._records) == 1
    await collector._record(normalize_binance_message({**trade(), "t": 2}, symbol="BTCUSDT",
                                                     received_ts_ns=_NOW + 2))
    await collector.flush()
    assert len(collector._records) == 1
    await collector.flush()
    assert notifications[0] == notifications[1] != notifications[2]
    assert [event.sequence for identity in notifications[1:] for event in registry.events(identity)] == [1, 2]
    assert not collector._records and collector._record_bytes == 0


@pytest.mark.asyncio
async def test_cancelled_flush_awaits_inflight_publication_before_retry(tmp_path, monkeypatch):
    registry, notifications = DatasetRegistry(tmp_path), []
    collector = BinanceCollector(registry, clock=clock(), on_manifest=notifications.append)
    await collector._record(normalize_binance_message(trade(), symbol="BTCUSDT", received_ts_ns=_NOW + 1))
    started, release = threading.Event(), threading.Event()
    original, calls = registry.register_table, []

    def register(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "register_table", register)
    task = asyncio.create_task(collector.flush())
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.wait_for(collector.flush(), timeout=2)
    assert len(calls) == len(notifications) == 1
    assert not collector._records
