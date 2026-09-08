# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Binance USD-M public streams and low-frequency REST pollers."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

try:
    import websockets
except ModuleNotFoundError:  # optional live-feed dependency
    websockets = None  # type: ignore[assignment]

from ..models import BookTicker, Candle, ForceOrder, MarkPrice, OISnapshot
from .binance_rest import BinanceRest

log = logging.getLogger(__name__)


class BinanceWs:
    def __init__(self, symbol: str, queue: asyncio.Queue):
        self.symbol = symbol.lower()
        self.queue = queue
        self.last_msg_monotonic = 0.0
        self.last_drop_monotonic = 0.0
        self.drop_count = 0
        self._stop = False

    def stop(self):
        self._stop = True

    async def _put(self, item):
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.last_drop_monotonic = time.monotonic()
            self.drop_count += 1
            log.error("market queue full; dropping %s", item[0])

    async def run(self):
        streams = "/".join([
            f"{self.symbol}@kline_1m", f"{self.symbol}@aggTrade",
            f"{self.symbol}@bookTicker", f"{self.symbol}@markPrice@1s",
            f"{self.symbol}@forceOrder",
        ])
        url = f"wss://fstream.binance.com/stream?streams={streams}"
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              close_timeout=5, max_queue=4096) as ws:
                    backoff = 1.0
                    async for raw in ws:
                        self.last_msg_monotonic = time.monotonic()
                        msg = json.loads(raw).get("data", {})
                        await self._dispatch(msg)
                        if self._stop:
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Binance WS reconnecting: %s", exc)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 30.0)

    async def _dispatch(self, d: dict):
        e = d.get("e")
        if e == "kline" and d.get("k", {}).get("x"):
            k = d["k"]
            await self._put(("candle", Candle(int(k["t"]), float(k["o"]), float(k["h"]),
                                               float(k["l"]), float(k["c"]), float(k["v"]))))
        elif e == "aggTrade":
            qty = float(d["q"])
            # m=True means buyer was maker -> aggressive sell.
            await self._put(("taker", (int(d["T"]), 0.0 if d["m"] else qty,
                                        qty if d["m"] else 0.0)))
            await self._put(("xprice", ("binance", int(d["T"]), float(d["p"]))))
        elif e == "bookTicker":
            await self._put(("book", BookTicker(int(d.get("E") or time.time() * 1000),
                                                 float(d["b"]), float(d["a"]))))
        elif e == "markPriceUpdate":
            await self._put(("mark", MarkPrice(int(d["E"]), float(d["p"]), float(d["i"]),
                                                float(d["r"]), int(d["T"]))))
        elif e == "forceOrder":
            o = d["o"]
            await self._put(("liq", ForceOrder(int(o["T"]), o["S"],
                                                float(o.get("ap") or o["p"]), float(o["z"]))))


class RestPollers:
    def __init__(self, rest: BinanceRest, symbol: str, queue: asyncio.Queue,
                 last_kline_ts: int = 0):
        self.rest, self.symbol, self.queue = rest, symbol, queue
        self.last_kline_ts = last_kline_ts
        self._stop = False

    def stop(self):
        self._stop = True

    async def run_oi(self):
        while not self._stop:
            try:
                d = await self.rest.open_interest(self.symbol)
                await self.queue.put(("oi", OISnapshot(int(d.get("time") or time.time() * 1000),
                                                       float(d["openInterest"]))))
            except Exception as exc:
                log.warning("OI poll failed: %s", exc)
            await asyncio.sleep(60)

    async def poll_klines_once(self, now_ms: int | None = None) -> int:
        """Queue every newly closed candle; REST is the WS gap/stall fallback."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        start_time = self.last_kline_ts + 60_000 if self.last_kline_ts else None
        rows = await self.rest.klines(
            self.symbol, start_time=start_time, limit=1500 if start_time else 5)
        queued = 0
        for row in rows:
            ts = int(row[0])
            if ts <= self.last_kline_ts or ts + 60_000 > now_ms:
                continue
            await self.queue.put(("candle", Candle(
                ts, float(row[1]), float(row[2]), float(row[3]),
                float(row[4]), float(row[5]))))
            self.last_kline_ts = ts
            queued += 1
        return queued

    async def run_klines(self):
        while not self._stop:
            try:
                queued = await self.poll_klines_once()
                if queued > 1:
                    log.warning("REST recovered %d missing minute candles", queued)
            except Exception as exc:
                log.warning("kline REST fallback failed: %s", exc)
            await asyncio.sleep(5)

    async def run_lsr(self):
        while not self._stop:
            try:
                d = await self.rest.long_short_ratio(self.symbol)
                if d:
                    await self.queue.put(("lsr", float(d["longShortRatio"])))
            except Exception as exc:
                log.warning("long/short ratio poll failed: %s", exc)
            await asyncio.sleep(300)
