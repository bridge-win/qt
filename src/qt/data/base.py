"""Adapter base utilities: HTTP client with retry, schema helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import pandas as pd
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from qt.core.logging import get_logger

log = get_logger(__name__)


DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=16),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
)
def http_get_json(
    url: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> Any:
    """GET with retry/backoff. Raises on final failure."""

    with httpx.Client(timeout=timeout, headers=headers) as client:
        r = client.get(url, params=dict(params or {}))
        r.raise_for_status()
        return r.json()


def empty_ohlcv() -> pd.DataFrame:
    """Empty DataFrame with the canonical OHLCV schema."""

    df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df.index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return df


def empty_series(name: str) -> pd.Series:
    s = pd.Series(dtype="float64", name=name)
    s.index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return s


def coerce_utc_index(df: pd.DataFrame, ts_col: str = "ts") -> pd.DataFrame:
    if ts_col in df.columns:
        df[ts_col] = pd.to_datetime(df[ts_col], utc=True)
        df = df.set_index(ts_col)
    elif not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index.name = "ts"
    return df.sort_index()
