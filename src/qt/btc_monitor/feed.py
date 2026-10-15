"""Binance spot public-data adapter. No account or authenticated endpoints exist here."""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import httpx
import websockets

from .config import MonitorConfig
from .models import MarketEvent


class FeedValidationError(ValueError):
    pass


class StreamProgress:
    """Per-channel silence watchdog, independent of transport pings or other streams."""

    def __init__(self, started: float, config: MonitorConfig):
        self.received = {"trade": started, "quote": started, "candle": started}
        self.deadlines = {
            "trade": max(10, config.trade_stale_seconds * 3),
            "quote": max(10, config.quote_stale_seconds * 3),
            "candle": max(150, config.candle_stale_seconds + 60),
        }

    def touch(self, channel: str, now: float) -> None:
        if channel in self.received:
            self.received[channel] = now

    def stalled(self, now: float) -> bool:
        return any(now - self.received[name] > limit for name, limit in self.deadlines.items())


def _number(value: Any, *, positive: bool = True) -> float:
    if isinstance(value, bool):
        raise FeedValidationError("boolean numeric field")
    result = float(value)
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise FeedValidationError("invalid numeric field")
    return result


def _sequence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise FeedValidationError("invalid sequence")
    result = int(value)
    if result < 0:
        raise FeedValidationError("invalid sequence")
    return result


def parse_message(
    raw: str | bytes | dict[str, Any], received_at: float, received_mono: float
) -> MarketEvent | None:
    """Use combined stream identity: spot bookTicker intentionally has no E/e fields."""
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        stream, data = payload["stream"], payload["data"]
        if not isinstance(data, dict) or not isinstance(stream, str):
            raise FeedValidationError("malformed combined stream")
        if stream == "btcusdt@aggTrade":
            if data.get("s") != "BTCUSDT" or data.get("e") != "aggTrade":
                raise FeedValidationError("unexpected trade identity")
            return MarketEvent(
                channel="trade",
                event_time=_number(data["T"]) / 1000,
                sequence=_sequence(data["a"]),
                price=_number(data["p"]),
                quantity=_number(data["q"], positive=False),
                received_at=received_at,
                received_mono=received_mono,
            )
        if stream == "btcusdt@bookTicker":
            if data.get("s") != "BTCUSDT":
                raise FeedValidationError("unexpected quote identity")
            bid, ask = _number(data["b"]), _number(data["a"])
            if ask < bid:
                raise FeedValidationError("crossed quote")
            return MarketEvent(
                channel="quote",
                event_time=None,
                sequence=_sequence(data["u"]),
                price=(bid + ask) / 2,
                bid=bid,
                ask=ask,
                received_at=received_at,
                received_mono=received_mono,
            )
        if stream == "btcusdt@kline_1m":
            candle = data["k"]
            if data.get("s") != "BTCUSDT" or candle.get("i") != "1m":
                raise FeedValidationError("unexpected candle identity")
            if candle.get("x") is not True:
                return None
            return _candle(
                candle["t"],
                candle["T"],
                candle["c"],
                candle["l"],
                candle["h"],
                received_at,
                received_mono,
                False,
                _number(data["E"]) / 1000,
            )
        raise FeedValidationError("unexpected stream")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FeedValidationError("invalid public market message") from exc


def _candle(
    open_ms: Any,
    close_ms: Any,
    close: Any,
    low: Any,
    high: Any,
    received_at: float,
    received_mono: float,
    historical: bool,
    event_time: float | None = None,
) -> MarketEvent:
    opened, closed = _number(open_ms) / 1000, _number(close_ms) / 1000
    price, bottom, top = _number(close), _number(low), _number(high)
    if not bottom <= price <= top or abs(closed - opened - 59.999) > 0.01:
        raise FeedValidationError("incoherent closed candle")
    return MarketEvent(
        channel="candle",
        event_time=event_time if event_time is not None else closed,
        received_at=received_at,
        received_mono=received_mono,
        sequence=int(open_ms),
        price=price,
        low=bottom,
        high=top,
        open_time=opened,
        close_time=closed,
        historical=historical,
    )


def parse_bootstrap(rows: Any, now: float, mono: float) -> list[MarketEvent]:
    if not isinstance(rows, list):
        raise FeedValidationError("bootstrap must be a list")
    result: list[MarketEvent] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise FeedValidationError("invalid bootstrap row")
        if _number(row[6]) / 1000 >= now:
            continue  # forming candles never enter finalized context
        item = _candle(row[0], row[6], row[4], row[3], row[2], now, mono, True)
        if result and item.sequence != result[-1].sequence + 60000:
            raise FeedValidationError("noncontiguous bootstrap")
        result.append(item)
    return result


async def run_feed(
    config: MonitorConfig,
    emit: Callable[[MarketEvent], None],
    reset: Callable[[str], None],
    stop: asyncio.Event,
    report_error: Callable[[str], None],
) -> None:
    """Ordinary exchange failures reconnect; validation never fabricates channel health."""
    backoff = 1.0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10), limits=httpx.Limits(max_connections=2)
    ) as client:
        while not stop.is_set():
            reset("reconnect_warmup")
            try:
                try:
                    response = await client.get(
                        f"{config.rest_url}/klines",
                        params={
                            "symbol": config.symbol,
                            "interval": "1m",
                            "limit": config.bootstrap_candles,
                        },
                    )
                    response.raise_for_status()
                    for historical_item in parse_bootstrap(
                        response.json(), time.time(), time.monotonic()
                    ):
                        emit(historical_item)
                except (httpx.HTTPError, FeedValidationError, ValueError):
                    # REST context failure must not disable independent realtime collection.
                    report_error("bootstrap_unavailable")
                async with websockets.connect(
                    config.websocket_url,
                    open_timeout=15,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=16,
                    max_size=65536,
                ) as socket:
                    started = time.monotonic()
                    progress = StreamProgress(started, config)
                    while (
                        not stop.is_set()
                        and time.monotonic() - started < config.session_rotation_seconds
                    ):
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=2)
                        except asyncio.TimeoutError:
                            if progress.stalled(time.monotonic()):
                                raise FeedValidationError("critical_channel_silent") from None
                            continue
                        wall, mono = time.time(), time.monotonic()
                        try:
                            item = parse_message(raw, wall, mono)
                        except FeedValidationError:
                            report_error("malformed_feed")
                            reset("malformed_feed")
                            continue
                        if item is not None:
                            progress.touch(item.channel, mono)
                            emit(item)
                        if progress.stalled(mono):
                            raise FeedValidationError("critical_channel_silent")
                        if mono - started >= 30:
                            backoff = 1.0
            except (
                OSError,
                httpx.HTTPError,
                websockets.exceptions.WebSocketException,
                FeedValidationError,
            ):
                report_error("feed_disconnected")
            if not stop.is_set():
                reset("feed_disconnected")
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=backoff + random.uniform(0, backoff / 4)
                    )
                backoff = min(backoff * 2, 60)
