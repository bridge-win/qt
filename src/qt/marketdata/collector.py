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
from decimal import Decimal
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


def _integer(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
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
    raw: dict[str, Any], *, symbol: str, received_ts_ns: int,
) -> MarketEvent | None:
    """Normalize one public frame; forming klines deliberately return None."""
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
        close_ns = (_integer(bar.get("T"), "bar_close") + 1) * 1_000_000
        volume, buy_volume = Decimal(bar["v"]), Decimal(bar["V"])
        from qt.marketdata.causal import ClosedBar

        closed = ClosedBar(
            instrument=symbol, open_ts_ns=_integer(bar.get("t"), "bar_open") * 1_000_000,
            close_ts_ns=close_ns, available_at_ns=max(close_ns, exchange_ns),
            received_at_ns=received_ts_ns, open=bar["o"], high=bar["h"], low=bar["l"],
            close=bar["c"], volume=volume,
        )
        if not 0 <= buy_volume <= volume:
            raise CollectorResyncError("invalid_kline_taker_volume")
        return MarketEvent(
            venue="binance", instrument=symbol, event_type="bar", exchange_ts_ns=close_ns,
            received_ts_ns=received_ts_ns, available_at_ns=closed.available_at_ns,
            payload=FrozenDict({**closed.model_dump(mode="json"), "interval": bar.get("i"),
                     "buy_volume": decimal_string(buy_volume),
                     "sell_volume": decimal_string(volume - buy_volume)}),
            source_hash=content_hash(message),
        )
    raise CollectorResyncError("unsupported_public_stream_event")


class BinanceCollector:
    """Bounded public stream -> verified order book -> callback + Parquet batches.

    The callback receives ``(event, book_or_none)``; the caller binds versioned
    instrument/fee specs before forwarding to TradingRuntime. Snapshot exchange
    time is unknown and marked explicitly. Unflushed batches remain in memory;
    their RPO is ``flush_interval_seconds`` or ``batch_size``, whichever occurs
    first. TradingRuntime independently checkpoints its exact decision inputs.
    """

    def __init__(
        self, registry: DatasetRegistry, *, symbol: str = "BTCUSDT", batch_size: int = 1000,
        maximum_buffer: int = 10_000, flush_interval_seconds: float = 1.0,
        maximum_book_age_ns: int = 1_000_000_000, bar_interval: str | None = "1m",
        on_event: Callable[[MarketEvent, BookView | None], Awaitable[None]] | None = None,
        on_health: Callable[[dict[str, Any]], Any] | None = None,
        on_manifest: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        if not re.fullmatch(r"[A-Z0-9]{5,24}", symbol):
            raise ValueError("invalid_spot_symbol")
        if not 1 <= batch_size <= maximum_buffer <= 100_000:
            raise ValueError("invalid_collector_batch_or_buffer_bound")
        if not 0.01 <= flush_interval_seconds <= 30 or maximum_book_age_ns <= 0:
            raise ValueError("invalid_collector_flush_or_freshness_bound")
        if bar_interval not in {None, "1m", "5m", "15m", "1h", "4h", "1d"}:
            raise ValueError("unsupported_closed_bar_interval")
        self.registry, self.symbol, self.batch_size = registry, symbol, batch_size
        self.maximum_buffer, self.flush_interval_seconds = maximum_buffer, flush_interval_seconds
        self.maximum_book_age_ns, self.bar_interval = maximum_book_age_ns, bar_interval
        self.on_event, self.on_health, self.on_manifest, self.clock = on_event, on_health, on_manifest, clock
        self.book = OrderBook()
        self.session_id = uuid.uuid4().hex
        self._records: list[dict[str, Any]] = []
        self._last_received_ns = 0
        self._last_flush = time.monotonic()
        self._last_health = 0.0

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
        result = self.on_health({"venue": "binance", "instrument": self.symbol, "status": status,
                                 "reason": reason, "book_valid": self.book.is_valid,
                                 "last_received_ns": self._last_received_ns,
                                 "buffered_events": len(self._records), "session_id": self.session_id})
        if inspect.isawaitable(result):
            await result

    async def _record(self, event: MarketEvent) -> None:
        if event.received_ts_ns < self._last_received_ns:
            raise CollectorResyncError("local_receipt_clock_regressed")
        self._last_received_ns = event.received_ts_ns
        self._records.append({"received_ts_ns": event.received_ts_ns,
                              "exchange_ts_ns": event.exchange_ts_ns,
                              "event_type": event.event_type, "sequence": event.sequence,
                              "event_json": event.model_dump_json()})
        if (len(self._records) >= self.batch_size
                or time.monotonic() - self._last_flush >= self.flush_interval_seconds):
            await self.flush()

    async def flush(self) -> None:
        if not self._records:
            return
        captured = list(self._records)
        table = pa.Table.from_pylist(captured, schema=pa.schema([
            ("received_ts_ns", pa.int64()), ("exchange_ts_ns", pa.int64()),
            ("event_type", pa.string()), ("sequence", pa.int64()), ("event_json", pa.string()),
        ]))
        manifest = await asyncio.to_thread(
            self.registry.register_table, table, venue="binance", instrument=self.symbol,
            data_type="market_events", timestamp_column="received_ts_ns", availability="received",
            rights="Binance public market data; confirm applicable use terms before redistribution",
            provenance={"source": "binance_spot_public_websocket", "stream_url": self.stream_url,
                        "rest_snapshot": "https://api.binance.com/api/v3/depth", "session_id": self.session_id,
                        "receipt_clock": "unix_nanoseconds", "snapshot_exchange_time": "unknown"},
        )
        del self._records[:len(captured)]
        self._last_flush = time.monotonic()
        if self.on_manifest is not None:
            result = self.on_manifest(manifest.model_dump(mode="json"))
            if inspect.isawaitable(result):
                await result

    async def _emit(self, event: MarketEvent) -> None:
        view = None
        was_valid = self.book.is_valid
        if was_valid:
            try:
                view = self.book.view(now_ns=self.clock(), maximum_age_ns=self.maximum_book_age_ns,
                                      depth=5000)
            except BookInvalidError:
                self.book.invalidate("stale_book")
        if self.on_event is not None:
            await self.on_event(event, view)
        await self._health("ready" if self.book.is_valid else "syncing")
        if was_valid and self.book.status == "INVALID":
            raise CollectorResyncError(self.book.invalid_reason or "invalid_book")

    async def _session(
        self, stream: AsyncIterator[str | bytes], snapshot_fetcher: Callable[[], Awaitable[dict[str, Any]]],
        stop: asyncio.Event,
    ) -> None:
        queue: asyncio.Queue[tuple[str, int, Any]] = asyncio.Queue(maxsize=self.maximum_buffer)
        producer_error: list[Exception] = []
        buffered: list[MarketEvent] = []
        self.book.invalidate("connection_resync")

        async def produce() -> None:
            try:
                async for raw in stream:
                    if stop.is_set():
                        return
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    if not isinstance(raw, str) or len(raw.encode()) > 1_048_576:
                        raise CollectorResyncError("frame_size_or_type_invalid")
                    item = json.loads(raw)
                    queue.put_nowait(("frame", self.clock(), item))
            except Exception as exc:
                producer_error.append(exc)
                self.book.invalidate("reader_failed_or_buffer_overflow")

        async def fetch_snapshot() -> None:
            try:
                payload = await snapshot_fetcher()
                queue.put_nowait(("snapshot", self.clock(), payload))
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
                try:
                    category, received_ns, raw = await asyncio.wait_for(queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    await self.flush()
                    await self._health("stale", "stream_idle", force=True)
                    if self._last_received_ns and self.clock() - self._last_received_ns > 5_000_000_000:
                        raise CollectorResyncError("stream_idle_timeout") from None
                    continue
                if category == "snapshot":
                    if not isinstance(raw, dict):
                        raise CollectorResyncError("invalid_depth_snapshot")
                    snapshot = DepthSnapshot(
                        last_update_id=_integer(raw.get("lastUpdateId"), "snapshot_sequence"),
                        bids=_levels(raw.get("bids")), asks=_levels(raw.get("asks")),
                        exchange_ts_ns=0, received_ts_ns=received_ns,
                    )
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
                    continue
                event = normalize_binance_message(raw, symbol=self.symbol, received_ts_ns=received_ns)
                if event is None:
                    continue
                await self._record(event)
                if event.event_type == "delta":
                    if snapshot_task is None:
                        snapshot_task = asyncio.create_task(fetch_snapshot())
                    if self.book.last_update_id is None or self.book.status == "INVALID":
                        buffered.append(event)
                        if len(buffered) >= self.maximum_buffer:
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

        @asynccontextmanager
        async def public_stream() -> AsyncIterator[Any]:
            import websockets

            async with websockets.connect(self.stream_url, ping_interval=20, ping_timeout=20,
                                          close_timeout=5, max_size=1_048_576, max_queue=16) as socket:
                yield socket

        async def public_snapshot() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get("https://api.binance.com/api/v3/depth",
                                            params={"symbol": self.symbol, "limit": 5000})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise CollectorResyncError("snapshot_response_is_not_object")
                return dict(payload)

        factory, fetcher = stream_factory or public_stream, snapshot_fetcher or public_snapshot
        sessions = 0
        while not stop.is_set() and (maximum_sessions is None or sessions < maximum_sessions):
            self.session_id = uuid.uuid4().hex
            sessions += 1
            try:
                async with factory() as stream:
                    await self._session(stream, fetcher, stop)
            except asyncio.CancelledError:
                await self._health("stopped", "cancelled", force=True)
                raise
            except (CollectorResyncError, OSError, httpx.HTTPError, ValueError) as exc:
                self.book.invalidate(type(exc).__name__)
                await self._health("reconnecting", type(exc).__name__, force=True)
                if maximum_sessions is not None and sessions >= maximum_sessions:
                    break
                # Bounded exponential backoff; a stop request interrupts waiting.
                delay = min(60.0, float(2 ** min(sessions - 1, 6)))
                sleeper = asyncio.ensure_future(sleep(delay))
                stopper = asyncio.create_task(stop.wait())
                _done, pending = await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        await self.flush()
        await self._health("stopped", force=True)
