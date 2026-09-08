# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Small, rate-limited Binance USD-M public REST client."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp


class BinanceRest:
    BASE = "https://fapi.binance.com"

    def __init__(self, timeout_sec: float = 15.0):
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *_):
        if self.session:
            await self.session.close()

    async def get(self, path: str, params: dict[str, Any] | None = None,
                  retries: int = 5) -> Any:
        if self.session is None:
            raise RuntimeError("BinanceRest must be used as an async context manager")
        delay = 0.5
        for attempt in range(retries):
            async with self.session.get(self.BASE + path, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status in (418, 429, 500, 502, 503, 504):
                    if attempt + 1 == retries:
                        resp.raise_for_status()
                    retry = float(resp.headers.get("Retry-After", delay))
                    await asyncio.sleep(min(max(retry, delay), 30.0))
                    delay *= 2
                    continue
                body = await resp.text()
                raise RuntimeError(f"Binance {path} HTTP {resp.status}: {body[:300]}")
        raise RuntimeError(f"Binance {path} exhausted retries")

    async def klines(self, symbol: str, interval: str = "1m", start_time: int | None = None,
                     end_time: int | None = None, limit: int = 1500):
        p: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            p["startTime"] = start_time
        if end_time is not None:
            p["endTime"] = end_time
        return await self.get("/fapi/v1/klines", p)

    async def funding_history(self, symbol: str, start_time: int | None = None,
                              end_time: int | None = None, limit: int = 1000):
        p: dict[str, Any] = {"symbol": symbol, "limit": limit}
        if start_time is not None:
            p["startTime"] = start_time
        if end_time is not None:
            p["endTime"] = end_time
        return await self.get("/fapi/v1/fundingRate", p)

    async def open_interest(self, symbol: str):
        return await self.get("/fapi/v1/openInterest", {"symbol": symbol})

    async def long_short_ratio(self, symbol: str, period: str = "5m"):
        rows = await self.get("/futures/data/globalLongShortAccountRatio",
                              {"symbol": symbol, "period": period, "limit": 1})
        return rows[-1] if rows else None

    async def ping(self) -> int:
        await self.get("/fapi/v1/ping")
        return int(time.time() * 1000)


