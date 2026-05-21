"""Derivatives data: funding rates, open interest, liquidations.

Primary venue: Binance USD-M futures public REST. No auth required for the
history endpoints used here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from qt.core.logging import get_logger
from qt.data.base import coerce_utc_index, http_get_json

log = get_logger(__name__)

BINANCE_FAPI = "https://fapi.binance.com"


def fetch_funding_rate_history(
    symbol: str = "BTCUSDT",
    since: datetime | None = None,
    until: datetime | None = None,
) -> pd.DataFrame:
    """8h funding rate history. Columns: ['funding_rate']."""

    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=365)

    rows: list[dict[str, float | int]] = []
    cursor_ms = int(since.timestamp() * 1000)
    end_ms = int(until.timestamp() * 1000)
    while cursor_ms < end_ms:
        try:
            data = http_get_json(
                f"{BINANCE_FAPI}/fapi/v1/fundingRate",
                params={"symbol": symbol, "startTime": cursor_ms, "limit": 1000},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("funding_fetch_failed", error=str(e))
            break
        if not data:
            break
        rows.extend(data)
        last_ts = int(data[-1]["fundingTime"])
        if last_ts <= cursor_ms:
            break
        cursor_ms = last_ts + 1

    if not rows:
        return pd.DataFrame(columns=["funding_rate"])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["funding_rate"] = df["fundingRate"].astype(float)
    return coerce_utc_index(df[["ts", "funding_rate"]])


def fetch_open_interest_history(
    symbol: str = "BTCUSDT",
    period: str = "1h",
    since: datetime | None = None,
    until: datetime | None = None,
) -> pd.DataFrame:
    """OI in BTC and USD. Binance retains ~30d only."""

    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=30)
    try:
        data = http_get_json(
            f"{BINANCE_FAPI}/futures/data/openInterestHist",
            params={
                "symbol": symbol,
                "period": period,
                "startTime": int(since.timestamp() * 1000),
                "endTime": int(until.timestamp() * 1000),
                "limit": 500,
            },
        )
    except Exception as e:  # noqa: BLE001
        log.warning("oi_fetch_failed", error=str(e))
        return pd.DataFrame(columns=["oi_btc", "oi_usd"])
    if not data:
        return pd.DataFrame(columns=["oi_btc", "oi_usd"])
    df = pd.DataFrame(data)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["oi_btc"] = df["sumOpenInterest"].astype(float)
    df["oi_usd"] = df["sumOpenInterestValue"].astype(float)
    return coerce_utc_index(df[["ts", "oi_btc", "oi_usd"]])


def fetch_long_short_ratio(
    symbol: str = "BTCUSDT",
    period: str = "1h",
    since: datetime | None = None,
    until: datetime | None = None,
) -> pd.DataFrame:
    """Global account long/short ratio. Useful for sentiment confirmation."""

    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=30)
    try:
        data = http_get_json(
            f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
            params={
                "symbol": symbol,
                "period": period,
                "startTime": int(since.timestamp() * 1000),
                "endTime": int(until.timestamp() * 1000),
                "limit": 500,
            },
        )
    except Exception as e:  # noqa: BLE001
        log.warning("lsr_fetch_failed", error=str(e))
        return pd.DataFrame(columns=["long_short_ratio"])
    if not data:
        return pd.DataFrame(columns=["long_short_ratio"])
    df = pd.DataFrame(data)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["long_short_ratio"] = df["longShortRatio"].astype(float)
    return coerce_utc_index(df[["ts", "long_short_ratio"]])
