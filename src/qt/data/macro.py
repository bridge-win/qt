"""Macro context series: DXY, VIX, US 10Y, M2, gold, SPX.

Uses FRED for official macro and Yahoo Finance for market indices (DXY, VIX).
Yahoo via yfinance is optional — falls back to FRED-only data when unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from qt.core.logging import get_logger
from qt.data.base import coerce_utc_index, http_get_json

log = get_logger(__name__)

FRED = "https://api.stlouisfed.org/fred"

FRED_SERIES = {
    "us10y": "DGS10",
    "fed_funds": "DFF",
    "m2": "M2SL",
    "cpi": "CPIAUCSL",
    "dxy": "DTWEXBGS",
    "vix": "VIXCLS",
}


def fetch_fred(
    metric: str,
    api_key: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> pd.DataFrame:
    if not api_key:
        log.info("fred_no_key", metric=metric)
        return pd.DataFrame(columns=[metric])
    series_id = FRED_SERIES.get(metric, metric)
    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=365 * 5)
    try:
        data = http_get_json(
            f"{FRED}/series/observations",
            params={
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "observation_start": since.strftime("%Y-%m-%d"),
                "observation_end": until.strftime("%Y-%m-%d"),
            },
        )
    except Exception as e:  # noqa: BLE001
        log.warning("fred_failed", metric=metric, error=str(e))
        return pd.DataFrame(columns=[metric])
    obs = data.get("observations", [])
    if not obs:
        return pd.DataFrame(columns=[metric])
    df = pd.DataFrame(obs)
    df["ts"] = pd.to_datetime(df["date"], utc=True)
    df[metric] = pd.to_numeric(df["value"], errors="coerce")
    return coerce_utc_index(df[["ts", metric]].dropna())
