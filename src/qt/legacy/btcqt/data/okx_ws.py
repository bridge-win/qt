# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""OKX public ticker reference stream."""
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

log = logging.getLogger(__name__)


class OkxWs:
    URL = "wss://ws.okx.com:8443/ws/v5/public"

    def __init__(self, inst: str, queue: asyncio.Queue):
        self.inst, self.queue = inst, queue
        self.last_msg_monotonic = 0.0
        self._stop = False

    def stop(self):
        self._stop = True

    async def run(self):
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(self.URL, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": [
                        {"channel": "tickers", "instId": self.inst}]}))
                    backoff = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        for d in msg.get("data", []):
                            px = d.get("last")
                            if px:
                                self.last_msg_monotonic = time.monotonic()
                                await self.queue.put(("xprice", ("okx", int(d.get("ts", 0)),
                                                                 float(px))))
                        if self._stop:
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("OKX WS reconnecting: %s", exc)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 30.0)

