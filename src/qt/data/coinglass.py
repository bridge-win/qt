"""Coinglass v4 API: aggregated liquidations + cross-exchange OI/funding.

`https://open-api-v4.coinglass.com`. Most endpoints require an API key
(`CG-API-KEY` header). Free tier rate-limits sharply (~30 req/min) and
truncates historical lookback. We expose minimal helpers; for serious
historical liquidation analysis, the paid Pro tier or the user's
self-recorded WebSocket stream is required.

When `api_key` is empty the adapters return empty DataFrames and log a
warning — they do not raise, so the composite pipeline degrades cleanly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from qt.core.logging import get_logger
from qt.data.base import coerce_utc_index, http_get_json

log = get_logger(__name__)

COINGLASS = "https://open-api-v4.coinglass.com/api"


def _hdr(api_key: str) -> dict[str, str]:
    return {"CG-API-KEY": api_key} if api_key else {}


def fetch_aggregated_liquidations(
    api_key: str,
    symbol: str = "BTC",
    interval: str = "1h",
    since: datetime | None = None,
    until: datetime | None = None,
) -> pd.DataFrame:
    """Aggregate long/short liquidations across all venues.

    Returns columns ``long_liq_usd``, ``short_liq_usd``.
    """

    if not api_key:
        log.info("coinglass_no_key", endpoint="liquidations")
        return pd.DataFrame(columns=["long_liq_usd", "short_liq_usd"])
    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=30)
    try:
        data = http_get_json(
            f"{COINGLASS}/futures/liquidation/aggregated-history",
            params={
                "symbol": symbol, "interval": interval,
                "start_time": int(since.timestamp() * 1000),
                "end_time": int(until.timestamp() * 1000),
                "limit": 4500,
            },
            headers=_hdr(api_key),
        )
    except Exception as e:
        log.warning("coinglass_liq_failed", error=str(e))
        return pd.DataFrame(columns=["long_liq_usd", "short_liq_usd"])
    rows = data.get("data") or []
    if not rows:
        return pd.DataFrame(columns=["long_liq_usd", "short_liq_usd"])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df["long_liq_usd"] = pd.to_numeric(df.get("long_liquidation_usd"), errors="coerce")
    df["short_liq_usd"] = pd.to_numeric(df.get("short_liquidation_usd"), errors="coerce")
    return coerce_utc_index(df[["ts", "long_liq_usd", "short_liq_usd"]])


def fetch_aggregated_funding(
    api_key: str, symbol: str = "BTC", interval: str = "1h",
    since: datetime | None = None, until: datetime | None = None,
) -> pd.DataFrame:
    """Cross-exchange aggregated funding rate (OI-weighted)."""

    if not api_key:
        log.info("coinglass_no_key", endpoint="funding")
        return pd.DataFrame(columns=["funding_agg"])
    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=30)
    try:
        data = http_get_json(
            f"{COINGLASS}/futures/funding-rate/oi-weighted-history",
            params={
                "symbol": symbol, "interval": interval,
                "start_time": int(since.timestamp() * 1000),
                "end_time": int(until.timestamp() * 1000),
                "limit": 4500,
            },
            headers=_hdr(api_key),
        )
    except Exception as e:
        log.warning("coinglass_funding_failed", error=str(e))
        return pd.DataFrame(columns=["funding_agg"])
    rows = data.get("data") or []
    if not rows:
        return pd.DataFrame(columns=["funding_agg"])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df["funding_agg"] = pd.to_numeric(df.get("value"), errors="coerce")
    return coerce_utc_index(df[["ts", "funding_agg"]])
