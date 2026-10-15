from __future__ import annotations

import pytest

from qt.btc_monitor.feed import FeedValidationError, parse_bootstrap, parse_message

NOW = 1_800_000_000.0


def combined(stream, data):
    return {"stream": f"btcusdt@{stream}", "data": data}


def test_spot_bookticker_without_event_type_or_time():
    event = parse_message(
        combined("bookTicker", {"s": "BTCUSDT", "u": 100, "b": "99999", "a": "100001"}), NOW, 1000
    )
    assert event.channel == "quote" and event.event_time is None
    assert event.price == 100000


def test_trade_identity_and_source_timestamp():
    event = parse_message(
        combined(
            "aggTrade",
            {
                "s": "BTCUSDT",
                "e": "aggTrade",
                "a": 1,
                "T": int(NOW * 1000),
                "p": "100000",
                "q": "1",
            },
        ),
        NOW,
        1000,
    )
    assert event.channel == "trade" and event.event_time == NOW
    assert event.received_mono == 1000


@pytest.mark.parametrize(
    "data",
    [
        {"s": "ETHUSDT", "u": 1, "b": "1", "a": "2"},
        {"s": "BTCUSDT", "u": 1, "b": "NaN", "a": "2"},
        {"s": "BTCUSDT", "u": 1, "b": "3", "a": "2"},
        {"s": "BTCUSDT", "u": -1, "b": "1", "a": "2"},
        {"s": "BTCUSDT", "u": 1, "b": True, "a": "2"},
    ],
)
def test_malformed_quotes_rejected(data):
    with pytest.raises(FeedValidationError):
        parse_message(combined("bookTicker", data), NOW, 1000)


def test_unknown_stream_and_non_json_rejected():
    for raw in ["not-json", combined("depth", {})]:
        with pytest.raises(FeedValidationError):
            parse_message(raw, NOW, 1000)


def row(opened):
    return [
        int(opened * 1000),
        "100000",
        "100100",
        "99900",
        "100000",
        "1",
        int((opened + 59.999) * 1000),
    ]


def test_bootstrap_only_closed_contiguous_history():
    events = parse_bootstrap([row(NOW - 120), row(NOW - 60), row(NOW)], NOW, 1000)
    assert len(events) == 2
    assert all(item.historical for item in events)
    with pytest.raises(FeedValidationError):
        parse_bootstrap([row(NOW - 180), row(NOW - 60)], NOW, 1000)


def test_forming_kline_ignored_closed_validated():
    data = {"s": "BTCUSDT", "E": int(NOW * 1000), "k": {"i": "1m", "x": False}}
    assert parse_message(combined("kline_1m", data), NOW, 1000) is None
    data["k"].update(
        x=True,
        t=int((NOW - 60) * 1000),
        T=int((NOW - 0.001) * 1000),
        c="100000",
        l="99900",
        h="100100",
    )
    assert parse_message(combined("kline_1m", data), NOW, 1000).historical is False


def test_channel_watchdog_trade_silence_not_masked_by_quotes():
    from qt.btc_monitor.config import MonitorConfig
    from qt.btc_monitor.feed import StreamProgress

    progress = StreamProgress(1000, MonitorConfig())
    progress.touch("quote", 1020)
    assert progress.stalled(1020)
    progress.touch("trade", 1020)
    assert not progress.stalled(1020)


def test_closed_candle_silence_reconnects_even_with_other_channels_alive():
    from qt.btc_monitor.config import MonitorConfig
    from qt.btc_monitor.feed import StreamProgress

    progress = StreamProgress(1000, MonitorConfig())
    progress.touch("trade", 1200)
    progress.touch("quote", 1200)
    assert progress.stalled(1200)
    progress.touch("candle", 1200)
    assert not progress.stalled(1200)


@pytest.mark.parametrize("sequence", [True, 1.5, -1])
def test_fractional_boolean_negative_sequence_rejected(sequence):
    with pytest.raises(FeedValidationError):
        parse_message(
            combined("bookTicker", {"s": "BTCUSDT", "u": sequence, "b": "99999", "a": "100001"}),
            NOW,
            1000,
        )


@pytest.mark.asyncio
async def test_rest_bootstrap_outage_does_not_block_websocket(monkeypatch):
    import asyncio
    import json

    import httpx

    from qt.btc_monitor import feed
    from qt.btc_monitor.config import MonitorConfig

    stop = asyncio.Event()
    received, errors = [], []

    class FailedRest:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("simulated REST-only outage")

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def recv(self):
            stop.set()
            return json.dumps(
                combined(
                    "aggTrade",
                    {
                        "s": "BTCUSDT",
                        "e": "aggTrade",
                        "a": 1,
                        "T": int(NOW * 1000),
                        "p": "100000",
                        "q": "1",
                    },
                )
            )

    monkeypatch.setattr(feed.httpx, "AsyncClient", FailedRest)
    monkeypatch.setattr(feed.websockets, "connect", lambda *args, **kwargs: Socket())
    await feed.run_feed(MonitorConfig(), received.append, lambda reason: None, stop, errors.append)
    assert errors == ["bootstrap_unavailable"]
    assert received[0].channel == "trade"
