from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import httpx
import pytest
from pydantic import ValidationError

from qt.btc_monitor.config import MonitorConfig
from qt.btc_monitor.engine import MonitorEngine
from qt.btc_monitor.feed import FeedValidationError
from qt.btc_monitor.models import MonitorSnapshot, Source
from qt.btc_monitor.okx import parse_okx_bootstrap, parse_okx_message, run_okx_feed
from qt.btc_monitor.service import MonitorService
from qt.btc_monitor.venues import SOURCES

NOW = 1_800_000_000.0


@pytest.mark.parametrize("key,expected", SOURCES.items())
def test_official_venue_environment_defaults(key, expected):
    config = MonitorConfig(venue=key[0], environment=key[1])
    source = Source.for_config(config)
    for field in ("symbol", "rest_url", "websocket_url"):
        assert getattr(config, field) == getattr(expected, field)
        assert getattr(source, field) == getattr(expected, field)
    assert source.exchange == expected.exchange and source.environment == expected.environment
    assert config.tick_size == (0.1 if expected.exchange == "okx" else 0.01)
    assert MonitorEngine(config).snapshot(NOW, 1000).assessment.source == source


def test_environment_defaults_have_separate_state_and_config_identity():
    configs = [MonitorConfig(venue=venue, environment=env) for venue, env in SOURCES]
    assert len({config.state_dir for config in configs}) == len(configs)
    assert len({config.version for config in configs}) == len(configs)


@pytest.mark.parametrize("kwargs", [
    {"venue": "polymarket"}, {"venue": "okx", "environment": "testnet"},
    {"venue": "binance", "environment": "demo"},
    {"venue": "okx", "rest_url": "https://www.okx.com/api/v5"},
    {"environment": "testnet", "websocket_url": SOURCES["binance", "production"].websocket_url},
    {"venue": "okx", "symbol": "BTCUSDT"}, {"venue": "okx", "bootstrap_candles": 301},
    {"api_key": "not-a-real-key"},
])
def test_invalid_mixed_sources_rejected(kwargs):
    with pytest.raises((ValueError, ValidationError)):
        MonitorConfig(**kwargs)


def test_source_projection_rejects_mixed_endpoints():
    data = asdict(SOURCES["okx", "demo"])
    data.pop("business_websocket_url")
    data["exchange"] = data.pop("exchange")
    data["websocket_url"] = SOURCES["okx", "production"].websocket_url
    with pytest.raises(ValidationError):
        Source.model_validate(data)


def message(channel, rows, **extra):
    return {"arg": {"channel": channel, "instId": "BTC-USDT"}, "data": rows, **extra}


def trade(identity="100", stamp=NOW):
    return {"instId": "BTC-USDT", "tradeId": identity, "px": "100000", "sz": "0.1", "ts": str(int(stamp * 1000))}


def candle(opened, confirmed="1"):
    return [str(int(opened * 1000)), "100000", "100100", "99900", "100000", "1", "1", "100000", confirmed]


def test_okx_all_trades_use_trade_id_not_repeating_seqid():
    rows = [dict(trade("100"), seqId=5), dict(trade("101"), seqId=5)]
    events = parse_okx_message(message("trades-all", rows), NOW, 1000)
    assert [event.sequence for event in events] == [100, 101]
    assert all(event.quantity == 0.1 and event.event_time == NOW for event in events)


def test_okx_quote_uses_source_timestamp_and_closed_candles_only():
    quote = parse_okx_message(message("tickers", [{"instId": "BTC-USDT", "ts": str(int(NOW * 1000)), "bidPx": "99999", "askPx": "100001"}]), NOW, 1000)[0]
    assert quote.event_time == NOW and quote.price == 100000
    events = parse_okx_message(message("candle1m", [candle(NOW, "0"), candle(NOW - 60)]), NOW, 1000)
    assert len(events) == 1 and events[0].close_time == pytest.approx(NOW - 0.001)
    assert events[0].historical is False


@pytest.mark.parametrize("payload", [
    {}, [], {"event": "error", "code": "60012"},
    message("trades", [trade()]),
    {"arg": {"channel": "trades-all", "instId": "ETH-USDT"}, "data": [trade()]},
    message("trades-all", [dict(trade(), instId="ETH-USDT")]),
    message("trades-all", [dict(trade(), px="NaN")]),
    message("trades-all", [dict(trade(), tradeId=True)]),
    message("tickers", [{"instId": "BTC-USDT", "ts": "1", "bidPx": "3", "askPx": "2"}]),
    message("candle1m", [candle(NOW, "true")]),
])
def test_okx_malformed_or_wrong_identity_rejected(payload):
    with pytest.raises(FeedValidationError):
        parse_okx_message(payload, NOW, 1000)


def test_okx_bootstrap_sorts_reverse_history_and_ignores_forming():
    events = parse_okx_bootstrap({"code": "0", "data": [candle(NOW, "0"), candle(NOW - 60), candle(NOW - 120)]}, NOW, 1000)
    assert [event.open_time for event in events] == [NOW - 120, NOW - 60]
    assert all(event.historical for event in events)
    for body in ({"code": "1", "data": []}, {"code": "0", "data": [candle(NOW - 180), candle(NOW - 60)]}, {"code": "0", "data": [[]]}):
        with pytest.raises(FeedValidationError):
            parse_okx_bootstrap(body, NOW, 1000)


def test_okx_conservative_gap_and_duplicate_detection_and_source_time():
    engine = MonitorEngine(MonitorConfig(venue="okx", environment="demo"))
    first = parse_okx_message(message("trades-all", [trade("100")]), NOW, 1000)[0]
    assert engine.ingest(first, NOW, 1000)
    assert not engine.ingest(first, NOW, 1000)
    later = parse_okx_message(message("trades-all", [trade("102", NOW + 1)]), NOW + 1, 1001)[0]
    assert engine.ingest(later, NOW + 1, 1001)
    assert engine.errors["trade_sequence_gap"] == 1
    quote = parse_okx_message(message("tickers", [{"instId": "BTC-USDT", "ts": str(int((NOW + 1) * 1000)), "bidPx": "99999", "askPx": "100001"}]), NOW + 1, 1001)[0]
    engine.ingest(quote, NOW + 1, 1001)
    assert engine.snapshot(NOW + 1, 1001).health.channels["quote"].source_time_known


def test_restore_rejects_different_source_and_projection_mixed_assessment():
    binance = MonitorEngine(MonitorConfig()).snapshot(NOW, 1000)
    okx = MonitorEngine(MonitorConfig(venue="okx", environment="demo"))
    okx.restore(binance, NOW, 1000)
    assert okx.errors["checkpoint_source_mismatch"] == 1
    mixed = binance.model_dump()
    mixed["source"] = okx.source.model_dump()
    with pytest.raises(ValidationError):
        MonitorSnapshot.model_validate(mixed)


@pytest.mark.asyncio
async def test_explicit_same_state_path_refuses_source_switch_without_overwrite(tmp_path):
    original = MonitorEngine(MonitorConfig(state_dir=tmp_path)).snapshot(NOW, 1000).model_dump_json()
    (tmp_path / "latest.json").write_text(original)
    service = MonitorService(MonitorConfig(state_dir=tmp_path, venue="okx", environment="demo"))
    with pytest.raises(RuntimeError, match="state_source_mismatch"):
        await service.run()
    assert (tmp_path / "latest.json").read_text() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["production", "demo"])
async def test_okx_rest_header_and_sockets_share_epoch(monkeypatch, environment):
    from qt.btc_monitor import okx

    stop = asyncio.Event()
    requests, urls, stopped, resets = [], [], [], []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            requests.append((url, kwargs))
            return httpx.Response(200, json={"code": "0", "data": []}, request=httpx.Request("GET", url))

    async def session(url, channels, config, emit, progress, stopped_event):
        urls.append(url)
        try:
            await asyncio.sleep(0)
            if "public" in url:
                stop.set()
                raise FeedValidationError("fixture_disconnect")
            await asyncio.Event().wait()
        finally:
            stopped.append(url)

    monkeypatch.setattr(okx.httpx, "AsyncClient", Client)
    monkeypatch.setattr(okx, "_socket_session", session)
    await run_okx_feed(MonitorConfig(venue="okx", environment=environment), lambda event: None, resets.append, stop, lambda reason: None)
    assert len(urls) == len(stopped) == 2
    assert requests[0][1]["headers"] == ({"x-simulated-trading": "1"} if environment == "demo" else {})
    assert all(("wspap" in url) == (environment == "demo") for url in urls)
    assert resets[-1] == "feed_disconnected"


@pytest.mark.asyncio
async def test_okx_text_ping_pong_and_no_false_market_heartbeat(monkeypatch):
    from qt.btc_monitor import okx

    stop, sent = asyncio.Event(), []
    clock = [1000.0]

    class Progress:
        def stalled(self, now):
            return False

        def touch(self, *args):
            pytest.fail("text pong must not refresh market channel")

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, value):
            sent.append(value)

        async def recv(self):
            clock[0] += 16
            if len(sent) == 1:
                raise asyncio.TimeoutError
            stop.set()
            return "pong"

    monkeypatch.setattr(okx.websockets, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(okx.time, "monotonic", lambda: clock[0])
    await okx._socket_session("wss://fixture", frozenset({"tickers"}), MonitorConfig(venue="okx"), lambda event: None, Progress(), stop)
    assert json.loads(sent[0])["op"] == "subscribe" and sent[1] == "ping"
