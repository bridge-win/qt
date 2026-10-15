"""OKX keyless spot feed. Both sockets share a single reconnect/health epoch."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import httpx
import websockets
from websockets.exceptions import WebSocketException

from .config import MonitorConfig
from .feed import FeedValidationError, StreamProgress, _candle, _number, _sequence
from .models import MarketEvent
from .venues import spot_source


def parse_okx_message(
    raw: str | bytes | dict[str, Any], received_at: float, received_mono: float,
    *, channels: frozenset[str] = frozenset({"trades-all", "tickers", "candle1m"}),
) -> list[MarketEvent]:
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(payload, dict):
            raise FeedValidationError("invalid OKX message")
        if payload.get("event") == "error":
            raise FeedValidationError("OKX subscription rejected")
        arg = payload["arg"]
        channel = arg["channel"]
        if arg.get("instId") != "BTC-USDT" or channel not in channels:
            raise FeedValidationError("unexpected OKX source identity")
        if payload.get("event") == "subscribe":
            return []
        data = payload["data"]
        if not isinstance(data, list) or not data:
            raise FeedValidationError("invalid OKX data batch")
        result: list[MarketEvent] = []
        for row in data:
            if channel == "candle1m":
                if not isinstance(row, list) or len(row) != 9 or row[8] not in {"0", "1"}:
                    raise FeedValidationError("invalid OKX candle")
                if row[8] == "1":
                    opened = _sequence(row[0])
                    result.append(_candle(
                        opened, opened + 59999, row[4], row[3], row[2],
                        received_at, received_mono, False,
                    ))
                continue
            if not isinstance(row, dict) or row.get("instId") != "BTC-USDT":
                raise FeedValidationError("unexpected OKX row identity")
            stamp = _sequence(row["ts"])
            if channel == "trades-all":
                result.append(MarketEvent(
                    channel="trade", event_time=stamp / 1000,
                    sequence=_sequence(row["tradeId"]), price=_number(row["px"]),
                    quantity=_number(row["sz"]), received_at=received_at,
                    received_mono=received_mono,
                ))
            else:
                bid, ask = _number(row["bidPx"]), _number(row["askPx"])
                if ask < bid:
                    raise FeedValidationError("crossed OKX quote")
                result.append(MarketEvent(
                    channel="quote", event_time=stamp / 1000, sequence=stamp,
                    price=(bid + ask) / 2, bid=bid, ask=ask,
                    received_at=received_at, received_mono=received_mono,
                ))
        return result
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FeedValidationError("invalid OKX public message") from exc


def parse_okx_bootstrap(payload: Any, now: float, mono: float) -> list[MarketEvent]:
    try:
        if not isinstance(payload, dict) or payload.get("code") != "0":
            raise FeedValidationError("OKX public API rejected request")
        rows = payload["data"]
        if not isinstance(rows, list) or len(rows) > 300:
            raise FeedValidationError("invalid OKX bootstrap")
        result: list[MarketEvent] = []
        for row in sorted(rows, key=lambda item: _sequence(item[0])):
            if not isinstance(row, list) or len(row) != 9 or row[8] not in {"0", "1"}:
                raise FeedValidationError("invalid OKX bootstrap row")
            opened = _sequence(row[0])
            if row[8] != "1" or (opened + 59999) / 1000 >= now:
                continue
            item = _candle(opened, opened + 59999, row[4], row[3], row[2], now, mono, True)
            if result and item.sequence != result[-1].sequence + 60000:
                raise FeedValidationError("noncontiguous OKX bootstrap")
            result.append(item)
        return result
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        raise FeedValidationError("invalid OKX bootstrap") from exc


async def _socket_session(
    url: str, channels: frozenset[str], config: MonitorConfig,
    emit: Callable[[MarketEvent], None], progress: StreamProgress, stop: asyncio.Event,
) -> None:
    # Close must fit inside the service's shutdown_seconds / 3 cancellation window.
    async with websockets.connect(
        url, open_timeout=15, close_timeout=min(1.0, config.shutdown_seconds / 6), ping_interval=None,
        max_queue=16, max_size=65536,
    ) as socket:
        await socket.send(json.dumps({
            "op": "subscribe", "args": [
                {"channel": channel, "instId": config.symbol} for channel in sorted(channels)
            ],
        }))
        started = last_receive = time.monotonic()
        ping_at: float | None = None
        while not stop.is_set() and time.monotonic() - started < config.session_rotation_seconds:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=2)
            except asyncio.TimeoutError:
                mono = time.monotonic()
                if progress.stalled(mono):
                    raise FeedValidationError("critical_channel_silent") from None
                if ping_at is not None and mono - ping_at > 10:
                    raise FeedValidationError("OKX heartbeat timeout") from None
                if ping_at is None and mono - last_receive >= 15:
                    await socket.send("ping")
                    ping_at = mono
                continue
            wall, mono = time.time(), time.monotonic()
            last_receive = mono
            if raw in ("pong", b"pong"):
                ping_at = None
                continue
            for item in parse_okx_message(raw, wall, mono, channels=channels):
                progress.touch(item.channel, mono)
                emit(item)
            if progress.stalled(mono):
                raise FeedValidationError("critical_channel_silent")


async def run_okx_feed(
    config: MonitorConfig, emit: Callable[[MarketEvent], None], reset: Callable[[str], None],
    stop: asyncio.Event, report_error: Callable[[str], None],
) -> None:
    source = spot_source(config.venue, config.environment)
    assert source.business_websocket_url is not None
    backoff = 1.0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10), limits=httpx.Limits(max_connections=2),
        follow_redirects=False,
    ) as client:
        while not stop.is_set():
            reset("reconnect_warmup")
            tasks: list[asyncio.Task[None]] = []
            started = time.monotonic()
            try:
                try:
                    response = await client.get(
                        f"{source.rest_url}/market/candles",
                        params={"instId": config.symbol, "bar": "1m", "limit": config.bootstrap_candles},
                        headers={"x-simulated-trading": "1"} if config.environment == "demo" else {},
                    )
                    response.raise_for_status()
                    for item in parse_okx_bootstrap(response.json(), time.time(), time.monotonic()):
                        emit(item)
                except (httpx.HTTPError, FeedValidationError, ValueError):
                    report_error("bootstrap_unavailable")
                progress = StreamProgress(time.monotonic(), config)
                tasks = [asyncio.create_task(_socket_session(
                    url, channels, config, emit, progress, stop,
                )) for url, channels in (
                    (source.websocket_url, frozenset({"tickers"})),
                    (source.business_websocket_url, frozenset({"trades-all", "candle1m"})),
                )]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if time.monotonic() - started >= 30:
                    backoff = 1.0
            except (OSError, httpx.HTTPError, WebSocketException, FeedValidationError):
                report_error("feed_disconnected")
            finally:
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                # Invalidate immediately, before reconnect delay, and only after both sockets stop.
                reset("feed_disconnected")
            if not stop.is_set():
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff + random.random())
                backoff = min(60.0, backoff * 2)
