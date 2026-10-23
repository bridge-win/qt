"""Public-feed fixtures exercise bootstrap, gaps, bounded buffering and replay."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from qt.datasets import DatasetRegistry
from qt.marketdata.collector import BinanceCollector, normalize_binance_message

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
