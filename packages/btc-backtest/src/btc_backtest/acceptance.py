"""Reusable acceptance helpers for real ten-year BTC backtests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx

from btc_backtest.data.cache import DataCache
from btc_backtest.data.models import DataRequest, MarketDataset, Timeframe
from btc_backtest.data.providers.base import ProviderRegistry
from btc_backtest.data.providers.bitstamp import BitstampProvider

BITSTAMP_TEN_YEAR_START = datetime(2016, 7, 25, tzinfo=timezone.utc)
BITSTAMP_TEN_YEAR_END = datetime(2026, 7, 25, tzinfo=timezone.utc)
BITSTAMP_TEN_YEAR_MAX_MISSING_RATIO = 0.001
_TIMEFRAME_SECONDS: dict[Timeframe, int] = {"1d": 86_400, "1h": 3_600}


def expected_slots(start: datetime, end: datetime, timeframe: Timeframe) -> int:
    """Return the expected closed-open bar count for an aligned interval."""

    start_utc = _as_utc(start)
    end_utc = _as_utc(end)
    if end_utc <= start_utc:
        raise ValueError("end must be after start")
    seconds = _TIMEFRAME_SECONDS[timeframe]
    duration = int((end_utc - start_utc).total_seconds())
    if duration % seconds:
        raise ValueError(f"interval is not aligned to {timeframe}")
    return duration // seconds


def ten_year_request(timeframe: Timeframe) -> DataRequest:
    """Build the reviewed Bitstamp BTC/USD ten-year acceptance request."""

    return DataRequest(
        provider="bitstamp",
        market="spot",
        symbol="BTC/USD",
        timeframe=timeframe,
        start=BITSTAMP_TEN_YEAR_START,
        end=BITSTAMP_TEN_YEAR_END,
        require_real=True,
        require_complete=False,
        max_missing_ratio=BITSTAMP_TEN_YEAR_MAX_MISSING_RATIO,
    )


def fetch_bitstamp_ten_year(
    timeframe: Timeframe,
    *,
    cache_dir: Path,
    timeout: float = 30.0,
) -> MarketDataset:
    """Fetch or load the ten-year real Bitstamp BTC/USD acceptance dataset."""

    request = ten_year_request(timeframe)
    cache = DataCache(cache_dir)
    with httpx.Client(timeout=timeout) as client:
        registry = ProviderRegistry([BitstampProvider(client)])
        return registry.fetch(request, cache)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
