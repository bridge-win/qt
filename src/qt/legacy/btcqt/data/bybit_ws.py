# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Bybit public full-liquidation and ticker streams."""
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

from ..models import ForceOrder

log = logging.getLogger(__name__)


class BybitWs:
    URL = "wss://stream.bybit.com/v5/public/linear"

    def __init__(self, symbol: str, queue: asyncio.Queue):
        self.symbol, self.queue = symbol.upper(), queue
        self.last_msg_monotonic = 0.0
        self._stop = False

    def stop(self):
        self._stop = True

    async def run(self):
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(self.URL, ping_interval=20, ping_timeout=20,
                                              max_queue=4096) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": [
                        f"allLiquidation.{self.symbol}", f"tickers.{self.symbol}"]}))
                    backoff = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        topic = msg.get("topic", "")
                        if not topic:
                            continue
                        self.last_msg_monotonic = time.monotonic()
                        if topic.startswith("allLiquidation."):
                            for d in msg.get("data", []):
                                # Bybit S is position side; normalize to liquidation order side.
                                order_side = "SELL" if d["S"].lower() == "buy" else "BUY"
                                await self.queue.put(("liq_bybit", ForceOrder(
                                    int(d["T"]), order_side, float(d["p"]), float(d["v"]))))
                        elif topic.startswith("tickers."):
                            d = msg.get("data") or {}
                            px = d.get("lastPrice") or d.get("markPrice")
                            if px:
                                await self.queue.put(("xprice", ("bybit", int(msg.get("ts", 0)),
                                                                 float(px))))
                        if self._stop:
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Bybit WS reconnecting: %s", exc)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 30.0)

