# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Data-freshness watchdog: if the market feed goes stale, cancel entry
orders, preserve exchange-side protection, block new entries, and alert.
Runs inside the app but independent of the trading loop's health."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(self, staleness_sec: float, check_interval_sec: float,
                 last_msg_monotonic: Callable[[], float],
                 on_stale: Callable[[], Awaitable[None]],
                 on_recovered: Callable[[], Awaitable[None]] | None = None):
        self.staleness = staleness_sec
        self.interval = check_interval_sec
        self.last_msg = last_msg_monotonic
        self.on_stale = on_stale
        self.on_recovered = on_recovered
        self.stale = False
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.interval)
            last = self.last_msg()
            if last <= 0:
                continue
            age = time.monotonic() - last
            if age > self.staleness and not self.stale:
                self.stale = True
                log.error("WATCHDOG: data stale for %.1fs — cancelling entry orders", age)
                try:
                    await self.on_stale()
                except Exception:
                    log.exception("watchdog on_stale failed")
            elif age <= self.staleness and self.stale:
                self.stale = False
                log.warning("watchdog: data recovered")
                if self.on_recovered:
                    try:
                        await self.on_recovered()
                    except Exception:
                        log.exception("watchdog on_recovered failed")

