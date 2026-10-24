"""Public Binance Spot collector with bounded bootstrap and immutable batches.

Only public depth, trades and optional closed UTC klines are used. No account
credentials, order methods or model providers exist in this module. Feed
frames retain receipt order; an overflow/gap disconnects and resynchronizes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from decimal import InvalidOperation, localcontext
from typing import Any

import httpx
import pyarrow as pa

from qt.contracts import FrozenDict, MarketEvent, canonical_json, content_hash, decimal_string
from qt.datasets import DatasetRegistry
from qt.marketdata.orderbook import (
    BookInvalidError,
    BookView,
    DepthDelta,
    DepthSnapshot,
    OrderBook,
    PriceLevel,
)


class CollectorResyncError(RuntimeError):
    """The current connection cannot provide a trustworthy contiguous book."""


class CollectorPersistenceError(RuntimeError):
    """Local publication failed; reconnecting the feed cannot repair it."""


class CollectorCallbackError(RuntimeError):
    """A downstream consumer failed; do not replay decisions as a network retry."""


def _integer(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise CollectorResyncError(f"invalid_binance_{name}")
    return value


def _levels(raw: object) -> tuple[PriceLevel, ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) > 5000:
        raise CollectorResyncError("invalid_or_excessive_depth_levels")
    levels = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise CollectorResyncError("invalid_depth_level")
        levels.append(PriceLevel(price=item[0], quantity=item[1]))
    return tuple(levels)


def normalize_binance_message(
    raw: object, *, symbol: str, received_ts_ns: int,
) -> MarketEvent | None:
    """Normalize one public frame; forming klines deliberately return None."""
    if not isinstance(raw, dict):
        raise CollectorResyncError("public_frame_is_not_object")
    message = raw.get("data", raw)
    if not isinstance(message, dict) or message.get("s") != symbol:
        raise CollectorResyncError("unexpected_stream_symbol")
    kind = message.get("e")
    exchange_ns = _integer(message.get("E"), "event_time") * 1_000_000
    if kind == "depthUpdate":
        delta = DepthDelta(
            first_update_id=_integer(message.get("U"), "first_update_id"),
            last_update_id=_integer(message.get("u"), "last_update_id"),
            bids=_levels(message.get("b")), asks=_levels(message.get("a")),
            exchange_ts_ns=exchange_ns, received_ts_ns=received_ts_ns,
        )
        return MarketEvent(
            venue="binance", instrument=symbol, event_type="delta", exchange_ts_ns=exchange_ns,
            received_ts_ns=received_ts_ns, sequence=delta.last_update_id,
            payload=FrozenDict(delta.model_dump(mode="json")), source_hash=content_hash(message),
        )
    if kind in {"trade", "aggTrade"}:
        if type(message.get("m")) is not bool:
            raise CollectorResyncError("invalid_trade_maker_flag")
        trade_id = _integer(message.get("t") if kind == "trade" else message.get("a"), "trade_id")
        level = PriceLevel.model_validate({"price": message.get("p"), "quantity": message.get("q")})
        if level.quantity == 0:
            raise CollectorResyncError("zero_trade_quantity")
        return MarketEvent(
            venue="binance", instrument=symbol, event_type="trade", exchange_ts_ns=exchange_ns,
            received_ts_ns=received_ts_ns, sequence=trade_id, source_hash=content_hash(message),
            payload=FrozenDict({"price": decimal_string(level.price), "quantity": decimal_string(level.quantity),
                     "trade_ts_ns": _integer(message.get("T"), "trade_time") * 1_000_000,
                     "aggressor_side": "sell" if message["m"] else "buy"}),
        )
    if kind == "kline":
        bar = message.get("k")
        if not isinstance(bar, dict) or type(bar.get("x")) is not bool:
            raise CollectorResyncError("invalid_kline")
        if not bar["x"]:
            return None
        intervals_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
        interval = bar.get("i")
        interval_ms = intervals_ms.get(interval) if isinstance(interval, str) else None
        open_ms = _integer(bar.get("t"), "bar_open")
        close_ms = _integer(bar.get("T"), "bar_close")
        if (interval_ms is None or close_ms + 1 - open_ms != interval_ms
                or bar.get("s", symbol) != symbol):
            raise CollectorResyncError("inconsistent_kline_identity_or_interval")
        close_ns = (close_ms + 1) * 1_000_000
        # Validate decimal strings before Decimal(): bool/float inputs and NaN
        # must not acquire an apparently legitimate decimal representation.
        volume = PriceLevel.model_validate({"price": "1", "quantity": bar.get("v")}).quantity
        buy_volume = PriceLevel.model_validate({"price": "1", "quantity": bar.get("V")}).quantity
        from qt.marketdata.causal import ClosedBar

        closed = ClosedBar.model_validate({
            "instrument": symbol, "open_ts_ns": open_ms * 1_000_000,
            "close_ts_ns": close_ns, "available_at_ns": max(close_ns, exchange_ns),
            "received_at_ns": received_ts_ns, "open": bar.get("o"), "high": bar.get("h"),
            "low": bar.get("l"), "close": bar.get("c"), "volume": volume,
        })
        if not 0 <= buy_volume <= volume:
            raise CollectorResyncError("invalid_kline_taker_volume")
        with localcontext() as context:
            context.prec = 80
            sell_volume = volume - buy_volume
        return MarketEvent(
            venue="binance", instrument=symbol, event_type="bar", exchange_ts_ns=close_ns,
            received_ts_ns=received_ts_ns, available_at_ns=closed.available_at_ns,
            payload=FrozenDict({**closed.model_dump(mode="json"), "interval": bar.get("i"),
                     "buy_volume": decimal_string(buy_volume),
                     "sell_volume": decimal_string(sell_volume)}),
            source_hash=content_hash(message),
        )
    raise CollectorResyncError("unsupported_public_stream_event")


class BinanceCollector:
    """Bounded public stream -> verified order book -> callback + Parquet batches.

    The callback receives ``(event, book_or_none)``; the caller binds versioned
    instrument/fee specs before forwarding to TradingRuntime. Snapshot exchange
    time is unknown and marked explicitly. Unflushed batches remain in memory;
    their RPO is ``flush_interval_seconds`` or ``batch_size``, whichever occurs
    first. A slow/unavailable publisher blocks the feed and ultimately forces a
    resync; it cannot grow an unbounded queue. Manifest notifications are at
    least once and consumers must deduplicate by dataset_id. TradingRuntime
    independently checkpoints its exact decision inputs.
    """

    def __init__(
        self, registry: DatasetRegistry, *, symbol: str = "BTCUSDT", batch_size: int = 1000,
        maximum_buffer: int = 10_000, flush_interval_seconds: float = 1.0,
        maximum_buffer_bytes: int = 16 * 1024 * 1024,
        maximum_book_age_ns: int = 1_000_000_000, bar_interval: str | None = "1m",
        idle_timeout_seconds: float = 5.0, bootstrap_timeout_seconds: float = 15.0,
        on_event: Callable[[MarketEvent, BookView | None], Awaitable[None]] | None = None,
        on_health: Callable[[dict[str, Any]], Any] | None = None,
        on_manifest: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        if not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
            raise ValueError("invalid_spot_symbol")
        if (type(batch_size) is not int or type(maximum_buffer) is not int
                or not 1 <= batch_size <= maximum_buffer <= 100_000):
            raise ValueError("invalid_collector_batch_or_buffer_bound")
        if (type(maximum_buffer_bytes) is not int
                or not 1024 <= maximum_buffer_bytes <= 256 * 1024 * 1024):
            raise ValueError("invalid_collector_byte_bound")
        if (not 0.01 <= flush_interval_seconds <= 30 or type(maximum_book_age_ns) is not int
                or maximum_book_age_ns <= 0):
            raise ValueError("invalid_collector_flush_or_freshness_bound")
        if not 0.01 <= idle_timeout_seconds <= 300 or not 0.01 <= bootstrap_timeout_seconds <= 300:
            raise ValueError("invalid_collector_connection_deadline")
        if bar_interval not in {None, "1m", "5m", "15m", "1h", "4h", "1d"}:
            raise ValueError("unsupported_closed_bar_interval")
        self.registry, self.symbol, self.batch_size = registry, symbol, batch_size
        self.maximum_buffer, self.flush_interval_seconds = maximum_buffer, flush_interval_seconds
        self.maximum_buffer_bytes = maximum_buffer_bytes
        self.idle_timeout_seconds, self.bootstrap_timeout_seconds = idle_timeout_seconds, bootstrap_timeout_seconds
        self.maximum_book_age_ns, self.bar_interval = maximum_book_age_ns, bar_interval
        self.on_event, self.on_health, self.on_manifest, self.clock = on_event, on_health, on_manifest, clock
        self.book = OrderBook()
        self.session_id = uuid.uuid4().hex
        self._records: list[dict[str, Any]] = []
        self._record_bytes = 0
        self._flush_lock = asyncio.Lock()
        self._registration: asyncio.Task[Any] | None = None
        self._pending_count = self._pending_bytes = 0
        self._last_received_ns = 0
        self._last_flush = time.monotonic()
        self._last_health = 0.0
        self._session_had_book = False

    @property
    def stream_url(self) -> str:
        symbol = self.symbol.lower()
        streams = f"{symbol}@depth@100ms/{symbol}@trade"
        if self.bar_interval:
            streams += f"/{symbol}@kline_{self.bar_interval}"
        return f"wss://stream.binance.com:9443/stream?streams={streams}"

    async def _health(self, status: str, reason: str | None = None, *, force: bool = False) -> None:
        if self.on_health is None or (not force and time.monotonic() - self._last_health < 1):
            return
        self._last_health = time.monotonic()
        try:
            result = self.on_health({"venue": "binance", "instrument": self.symbol, "status": status,
                                     "reason": reason, "book_valid": self.book.is_valid,
                                     "last_received_ns": self._last_received_ns,
                                     "buffered_events": len(self._records), "session_id": self.session_id})
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            raise CollectorCallbackError("health_publication_failed") from exc

    async def _record(self, event: MarketEvent) -> None:
        if event.received_ts_ns < self._last_received_ns:
            raise CollectorResyncError("local_receipt_clock_regressed")
        self._last_received_ns = event.received_ts_ns
        encoded = event.model_dump_json()
        size = len(encoded.encode("utf-8"))
        if size > self.maximum_buffer_bytes:
            raise CollectorResyncError("event_exceeds_persistence_buffer")
        if self._records and self._record_bytes + size > self.maximum_buffer_bytes:
            await self.flush()
        self._records.append({"received_ts_ns": event.received_ts_ns,
                              "exchange_ts_ns": event.exchange_ts_ns,
                              "event_type": event.event_type, "sequence": event.sequence,
                              "event_json": encoded})
        self._record_bytes += size
        if (len(self._records) >= self.batch_size
                or time.monotonic() - self._last_flush >= self.flush_interval_seconds):
            await self.flush()

    async def flush(self) -> None:
        async with self._flush_lock:
            if not self._records:
                return
            if self._registration is None:
                captured = list(self._records)
                self._pending_count, self._pending_bytes = len(captured), self._record_bytes
                table = pa.Table.from_pylist(captured, schema=pa.schema([
                    ("received_ts_ns", pa.int64()), ("exchange_ts_ns", pa.int64()),
                    ("event_type", pa.string()), ("sequence", pa.int64()), ("event_json", pa.string()),
                ]))
                # Retain this task across cancellation: to_thread cannot stop an
                # in-flight filesystem publication. A later flush must await the
                # same result before deciding whether to notify or retry.
                self._registration = asyncio.create_task(asyncio.to_thread(
                    self.registry.register_table, table, venue="binance", instrument=self.symbol,
                    data_type="market_events", timestamp_column="received_ts_ns", availability="received",
                    rights="Binance public market data; confirm applicable use terms before redistribution",
                    provenance={"source": "binance_spot_public_websocket", "stream_url": self.stream_url,
                                "rest_snapshot": "https://api.binance.com/api/v3/depth",
                                "session_id": self.session_id, "receipt_clock": "unix_nanoseconds",
                                "snapshot_exchange_time": "unknown"},
                ))
            try:
                manifest = await asyncio.shield(self._registration)
            except Exception as exc:
                self._registration = None
                raise CollectorPersistenceError("immutable_batch_publication_failed") from exc
            if self.on_manifest is not None:
                try:
                    result = self.on_manifest(manifest.model_dump(mode="json"))
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    # Keep both bytes and the already-published identity. The
                    # callback can safely be retried without another Parquet.
                    raise CollectorPersistenceError("manifest_notification_failed") from exc
            del self._records[:self._pending_count]
            self._record_bytes -= self._pending_bytes
            self._registration = None
            self._last_flush = time.monotonic()

    async def _emit(self, event: MarketEvent) -> None:
        view = None
        was_valid = self.book.is_valid
        if was_valid:
            try:
                view = self.book.view(now_ns=self.clock(), maximum_age_ns=self.maximum_book_age_ns,
                                      depth=5000)
                self._session_had_book = True
            except BookInvalidError:
                self.book.invalidate("stale_book")
        if self.on_event is not None:
            try:
                await self.on_event(event, view)
            except Exception as exc:
                raise CollectorCallbackError("event_consumer_failed") from exc
        await self._health("ready" if self.book.is_valid else "syncing")
        if was_valid and self.book.status == "INVALID":
            raise CollectorResyncError(self.book.invalid_reason or "invalid_book")

    async def _session(
        self, stream: AsyncIterator[str | bytes], snapshot_fetcher: Callable[[], Awaitable[dict[str, Any]]],
        stop: asyncio.Event,
    ) -> None:
        queue: asyncio.Queue[tuple[str, int, Any, int]] = asyncio.Queue(maxsize=self.maximum_buffer)
        producer_error: list[Exception] = []
        buffered: list[MarketEvent] = []
        buffered_bytes = queued_bytes = 0
        started = last_frame_at = time.monotonic()
        self.book.invalidate("connection_resync")
        self._session_had_book = False

        def enqueue(category: str, payload: Any, size: int) -> None:
            nonlocal queued_bytes
            if queued_bytes + size > self.maximum_buffer_bytes:
                raise CollectorResyncError("reader_byte_buffer_overflow")
            queue.put_nowait((category, self.clock(), payload, size))
            queued_bytes += size

        async def produce() -> None:
            nonlocal last_frame_at
            try:
                async for raw in stream:
                    if stop.is_set():
                        return
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    if not isinstance(raw, str):
                        raise CollectorResyncError("frame_size_or_type_invalid")
                    size = len(raw.encode("utf-8"))
                    if size > 1_048_576:
                        raise CollectorResyncError("frame_size_or_type_invalid")
                    # Keep bounded wire text in the queue. Parsing every frame
                    # in the producer can multiply memory before backpressure.
                    enqueue("frame", raw, size)
                    last_frame_at = time.monotonic()
            except Exception as exc:
                producer_error.append(exc)
                self.book.invalidate("reader_failed_or_buffer_overflow")

        async def fetch_snapshot() -> None:
            try:
                payload = await snapshot_fetcher()
                size = len(canonical_json(payload).encode("utf-8"))
                if size > 4 * 1_048_576:
                    raise CollectorResyncError("snapshot_size_invalid")
                enqueue("snapshot", payload, size)
            except Exception as exc:
                producer_error.append(exc)
                self.book.invalidate("snapshot_failed_or_buffer_overflow")

        reader = asyncio.create_task(produce())
        snapshot_task: asyncio.Task[None] | None = None
        try:
            while not stop.is_set():
                if producer_error:
                    raise CollectorResyncError(type(producer_error[0]).__name__) from producer_error[0]
                if reader.done() and queue.empty():
                    raise CollectorResyncError("websocket_disconnected")
                if not self.book.is_valid and time.monotonic() - started >= self.bootstrap_timeout_seconds:
                    raise CollectorResyncError("book_bootstrap_timeout")
                try:
                    category, received_ns, raw, size = await asyncio.wait_for(
                        queue.get(), timeout=min(1.0, self.idle_timeout_seconds, self.bootstrap_timeout_seconds),
                    )
                    queued_bytes -= size
                except asyncio.TimeoutError:
                    await self.flush()
                    await self._health("stale", "stream_idle", force=True)
                    # A brand-new socket with no frame must also time out. Use
                    # a monotonic connection clock, never the previous session.
                    if time.monotonic() - last_frame_at >= self.idle_timeout_seconds:
                        raise CollectorResyncError("stream_idle_timeout") from None
                    continue
                if category == "snapshot":
                    if not isinstance(raw, dict):
                        raise CollectorResyncError("invalid_depth_snapshot")
                    try:
                        snapshot = DepthSnapshot(
                            last_update_id=_integer(raw.get("lastUpdateId"), "snapshot_sequence"),
                            bids=_levels(raw.get("bids")), asks=_levels(raw.get("asks")),
                            exchange_ts_ns=0, received_ts_ns=received_ns,
                        )
                    except (ValueError, TypeError) as exc:
                        raise CollectorResyncError("malformed_depth_snapshot") from exc
                    await self._record(MarketEvent(
                        venue="binance", instrument=self.symbol, event_type="snapshot",
                        exchange_ts_ns=0, received_ts_ns=received_ns, sequence=snapshot.last_update_id,
                        payload=FrozenDict(snapshot.model_dump(mode="json")),
                        quality_flags=("EXCHANGE_TIME_UNKNOWN",), source_hash=content_hash(raw),
                    ))
                    if not self.book.apply_snapshot(snapshot):
                        raise CollectorResyncError(self.book.invalid_reason or "invalid_snapshot")
                    for buffered_event in buffered:
                        delta = DepthDelta.model_validate_json(canonical_json(buffered_event.payload))
                        if self.book.apply_delta(delta):
                            await self._emit(buffered_event)
                        elif self.book.status == "INVALID":
                            raise CollectorResyncError(self.book.invalid_reason or "bootstrap_gap")
                    buffered.clear()
                    buffered_bytes = 0
                    continue
                try:
                    event = normalize_binance_message(json.loads(raw), symbol=self.symbol,
                                                       received_ts_ns=received_ns)
                except (ValueError, TypeError, KeyError, InvalidOperation, RecursionError) as exc:
                    raise CollectorResyncError("malformed_public_frame") from exc
                if event is None:
                    continue
                if event.event_type == "bar" and event.payload.get("interval") != self.bar_interval:
                    raise CollectorResyncError("unexpected_closed_bar_interval")
                if event.exchange_ts_ns > received_ns:
                    raise CollectorResyncError("exchange_time_in_future")
                await self._record(event)
                if event.event_type == "delta":
                    if snapshot_task is None:
                        snapshot_task = asyncio.create_task(fetch_snapshot())
                    if self.book.last_update_id is None or self.book.status == "INVALID":
                        buffered.append(event)
                        buffered_bytes += len(event.model_dump_json().encode("utf-8"))
                        if (len(buffered) >= self.maximum_buffer
                                or buffered_bytes > self.maximum_buffer_bytes):
                            raise CollectorResyncError("bootstrap_buffer_overflow")
                        continue
                    applied = self.book.apply_delta(
                        DepthDelta.model_validate_json(canonical_json(event.payload)),
                    )
                    if self.book.invalid_reason not in (None, "bridging_delta_required"):
                        raise CollectorResyncError(self.book.invalid_reason or "sequence_gap")
                    if not applied:
                        continue
                await self._emit(event)
        finally:
            reader.cancel()
            if snapshot_task is not None:
                snapshot_task.cancel()
            await asyncio.gather(reader, *([snapshot_task] if snapshot_task is not None else []),
                                 return_exceptions=True)
            self.book.invalidate("connection_closed")
            await self.flush()

    async def run(
        self, *, stop: asyncio.Event | None = None, maximum_sessions: int | None = None,
        stream_factory: Callable[[], AbstractAsyncContextManager[AsyncIterator[str | bytes]]] | None = None,
        snapshot_fetcher: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        stop = stop or asyncio.Event()
        if maximum_sessions is not None and (type(maximum_sessions) is not int or maximum_sessions < 1):
            raise ValueError("maximum_sessions_must_be_positive_integer")
        from websockets.exceptions import WebSocketException

        @asynccontextmanager
        async def public_stream() -> AsyncIterator[Any]:
            import websockets

            async with websockets.connect(self.stream_url, ping_interval=20, ping_timeout=20,
                                          open_timeout=10, close_timeout=5,
                                          max_size=1_048_576, max_queue=16) as socket:
                yield socket

        async def public_snapshot() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=10) as client:
                async with client.stream("GET", "https://api.binance.com/api/v3/depth",
                                         params={"symbol": self.symbol, "limit": 5000}) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 4 * 1_048_576:
                            raise CollectorResyncError("snapshot_size_invalid")
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise CollectorResyncError("invalid_snapshot_json") from exc
                if not isinstance(payload, dict):
                    raise CollectorResyncError("snapshot_response_is_not_object")
                return dict(payload)

        factory, fetcher = stream_factory or public_stream, snapshot_fetcher or public_snapshot

        async def connect() -> None:
            async with factory() as stream:
                await self._session(stream, fetcher, stop)

        sessions = failures = 0
        while not stop.is_set() and (maximum_sessions is None or sessions < maximum_sessions):
            self.session_id = uuid.uuid4().hex
            sessions += 1
            session_started = time.monotonic()
            try:
                # Cancellation of the surrounding run or a stop request also
                # interrupts an initial DNS/TLS/WebSocket handshake.
                connection = asyncio.create_task(connect())
                stopper = asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait({connection, stopper}, return_when=asyncio.FIRST_COMPLETED)
                    if stop.is_set() and not connection.done():
                        connection.cancel()
                        await asyncio.gather(connection, return_exceptions=True)
                    else:
                        await connection
                finally:
                    for task in (connection, stopper):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(connection, stopper, return_exceptions=True)
            except asyncio.CancelledError:
                await self._health("stopped", "cancelled", force=True)
                raise
            except (CollectorResyncError, OSError, httpx.HTTPError, WebSocketException,
                    asyncio.TimeoutError) as exc:
                self.book.invalidate(type(exc).__name__)
                await self._health("reconnecting", type(exc).__name__, force=True)
                if maximum_sessions is not None and sessions >= maximum_sessions:
                    break
                # Bounded exponential backoff; a stop request interrupts waiting.
                if self._session_had_book and time.monotonic() - session_started >= 60:
                    failures = 0
                delay = min(60.0, float(2 ** min(failures, 6)))
                failures += 1
                sleeper = asyncio.ensure_future(sleep(delay))
                stopper = asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for pending_task in (sleeper, stopper):
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.gather(sleeper, stopper, return_exceptions=True)
        await self.flush()
        await self._health("stopped", force=True)
