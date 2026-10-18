"""Allowlisted public data only; incomplete/future bars never enter indicators."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import httpx

from .models import TIMEFRAMES, Bar, Policy, Quote
from .signals import validate_bars

BINANCE = "https://data-api.binance.vision/api/v3"
OKX = "https://openapi.okx.com/api/v5"


class PublicMarket:
    def __init__(self, monitor_root: Path | None = None, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time):
        self.client = client or httpx.Client(timeout=10, trust_env=False, follow_redirects=False)
        self.monitor_root = monitor_root
        self.clock = clock
        self.cache: dict[tuple[str, str], list[Bar]] = {}

    def close(self) -> None:
        self.client.close()

    def get(self, url: str, params: dict[str, str | int]) -> Any:
        if url not in {
            BINANCE + "/klines", BINANCE + "/ticker/bookTicker", OKX + "/market/candles",
            OKX + "/market/ticker", OKX + "/public/mark-price", OKX + "/public/funding-rate-history",
        }:
            raise ValueError("public_endpoint_not_allowed")
        response = self.client.get(url, params=params)
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError("market_response_too_large")
        return response.json()

    def bars(self, product: str, timeframe: str, now: float) -> list[Bar]:
        previous = self.cache.get((product, timeframe), [])
        seconds = TIMEFRAMES[timeframe]
        limit = 3 if previous and now - previous[-1].open_time < 3 * seconds else 300
        if product == "spot":
            data = self.get(BINANCE + "/klines", {"symbol": "BTCUSDT", "interval": timeframe, "limit": limit})
        else:
            value = self.get(OKX + "/market/candles", {
                "instId": "BTC-USDT-SWAP", "bar": {"15m": "15m", "1h": "1H", "4h": "4H"}[timeframe],
                "limit": limit,
            })
            if value.get("code") != "0":
                raise ValueError("public_provider_rejected")
            data = list(reversed(value["data"]))
        parsed = []
        for row in data:
            timestamp = int(row[0]) // 1000
            if timestamp + seconds > now:
                continue
            if product != "spot" and row[8] != "1":
                continue
            parsed.append(Bar(
                open_time=timestamp, open=float(row[1]), high=float(row[2]), low=float(row[3]),
                close=float(row[4]), volume=float(row[5]),
            ))
        if len({item.open_time for item in parsed}) != len(parsed):
            raise ValueError("duplicate_market_bars")
        if parsed != sorted(parsed, key=lambda item: item.open_time):
            raise ValueError("unordered_market_bars")
        merged = {item.open_time: item for item in previous}
        for item in parsed:
            if item.open_time in merged and item != merged[item.open_time]:
                self.cache.pop((product, timeframe), None)
                raise ValueError("closed_bar_revision_requires_research")
            merged[item.open_time] = item
        result = sorted(merged.values(), key=lambda item: item.open_time)[-300:]
        try:
            validate_bars(result, timeframe, now)
        except ValueError:
            # The next attempt must bootstrap full REST history, not repeat a
            # small incremental response that cannot repair an older gap.
            self.cache.pop((product, timeframe), None)
            raise
        self.cache[product, timeframe] = result
        return result

    def stream_ready(self, now: float) -> bool:
        if self.monitor_root is None:
            return False
        path = self.monitor_root / "latest.json"
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return False
        try:
            snapshot = json.loads(path.read_text())
            source = snapshot["source"]
            channels = snapshot["health"]["channels"]
            return bool(
                source["exchange"] == "binance" and source["environment"] == "production"
                and source["product"] == "spot" and source["symbol"] == "BTCUSDT"
                and 0 <= now - snapshot["published_at"] <= 5
                and channels["quote"]["ready"] and channels["trade"]["ready"]
                and 0 <= now - channels["quote"]["last_receive_at"] <= 5
                and 0 <= now - channels["trade"]["last_receive_at"] <= 5
            )
        except (KeyError, TypeError, ValueError, OSError):
            return False

    def quote(self, product: Literal["spot", "perpetual"], now: float, policy: Policy) -> Quote:
        if product == "spot":
            value = self.get(BINANCE + "/ticker/bookTicker", {"symbol": "BTCUSDT"})
            return Quote(venue="binance", product="spot", symbol="BTCUSDT",
                         bid=float(value["bidPrice"]), ask=float(value["askPrice"]), observed_at=self.clock())
        history = self.get(OKX + "/public/funding-rate-history", {"instId": "BTC-USDT-SWAP", "limit": 100})
        mark = self.get(OKX + "/public/mark-price", {"instType": "SWAP", "instId": "BTC-USDT-SWAP"})
        value = self.get(OKX + "/market/ticker", {"instId": "BTC-USDT-SWAP"})
        if any(item.get("code") != "0" or not item.get("data") for item in (value, mark, history)):
            raise ValueError("perpetual_context_unavailable")
        tick, mark_row, funding = value["data"][0], mark["data"][0], history["data"]
        times = sorted({int(item["fundingTime"]) / 1000 for item in funding})
        if len(times) < 2 or times[-1] > now or times[-1] - times[0] < policy.horizon_hours * 3600:
            raise ValueError("funding_context_insufficient")
        intervals = [b - a for a, b in pairwise(times)]
        if min(intervals) < 3600 or max(intervals) > 24 * 3600:
            raise ValueError("funding_interval_unknown")
        # Conservative scenario cost, not a claim to know future settlements.
        rates = [abs(float(item["realizedRate"])) for item in funding]
        if not all(math.isfinite(item) for item in rates):
            raise ValueError("nonfinite_funding")
        funding_cost = max(rates) * math.ceil(policy.horizon_hours * 3600 / min(intervals)) * 10000
        return Quote(
            venue="okx", product="perpetual", symbol="BTC-USDT-SWAP",
            bid=float(tick["bidPx"]), ask=float(tick["askPx"]), observed_at=self.clock(),
            source_at=min(int(tick["ts"]), int(mark_row["ts"])) / 1000,
            mark_price=float(mark_row["markPx"]), funding_cost_bps=funding_cost,
            funding_as_of=times[-1], funding_coverage_start=times[0],
        )

    def packet(self, product: Literal["spot", "perpetual"], now: float, policy: Policy) -> tuple[dict[str, list[Bar]], Quote]:
        bars = {timeframe: self.bars(product, timeframe, now) for timeframe in TIMEFRAMES}
        return bars, self.quote(product, now, policy)
