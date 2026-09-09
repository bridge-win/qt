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
    except Exception as e:
        log.warning("fred_failed", metric=metric, error=str(e))
        return pd.DataFrame(columns=[metric])
    obs = data.get("observations", [])
    if not obs:
        return pd.DataFrame(columns=[metric])
    df = pd.DataFrame(obs)
    df["ts"] = pd.to_datetime(df["date"], utc=True)
    df[metric] = pd.to_numeric(df["value"], errors="coerce")
    return coerce_utc_index(df[["ts", metric]].dropna())


# --- Yahoo Finance chart API (no key) ------------------------------------
#
# FRED has no true DXY (DTWEXBGS is the broad trade-weighted index, which
# moves differently). Yahoo's public chart endpoint exposes ``DX-Y.NYB``
# (ICE dollar index) and ``^VIX`` without authentication, so the live
# macro veto no longer needs a FRED key.

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_SYMBOLS = {"vix": "^VIX", "dxy": "DX-Y.NYB", "spx": "^GSPC", "gold": "GC=F"}
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; qt-research/1.0)"}


def fetch_yahoo_daily(metric: str, range_: str = "5y") -> pd.DataFrame:
    """Daily close for a Yahoo symbol; ``metric`` may be a key of
    ``YAHOO_SYMBOLS`` or a raw ticker. Returns a one-column frame named
    after ``metric``; empty on failure (never raises)."""

    ticker = YAHOO_SYMBOLS.get(metric, metric)
    try:
        data = http_get_json(
            f"{YAHOO_CHART}/{ticker}",
            params={"range": range_, "interval": "1d"},
            headers=_YAHOO_HEADERS,
        )
        result = data["chart"]["result"][0]
        ts = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except Exception as e:
        log.warning("yahoo_failed", metric=metric, ticker=ticker, error=str(e))
        return pd.DataFrame(columns=[metric])
    df = pd.DataFrame({"ts": pd.to_datetime(ts, unit="s", utc=True), metric: closes})
    df[metric] = pd.to_numeric(df[metric], errors="coerce")
    df = df.dropna()
    # normalise to midnight UTC so it aligns with other daily series
    df["ts"] = df["ts"].dt.floor("D")
    return coerce_utc_index(df.drop_duplicates("ts", keep="last"))


def fetch_macro_veto_inputs(fred_api_key: str = "") -> dict[str, pd.DataFrame]:
    """VIX + DXY with Yahoo first and FRED fallback (VIX only)."""

    out: dict[str, pd.DataFrame] = {}
    for m in ("vix", "dxy"):
        df = fetch_yahoo_daily(m)
        if df.empty and fred_api_key and m == "vix":
            df = fetch_fred("vix", api_key=fred_api_key)
        out[m] = df
    return out
