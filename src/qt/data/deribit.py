"""Deribit options public REST (no auth). https://www.deribit.com/api/v2

We expose the few endpoints needed for the bot:
- DVOL index (BTC implied vol index)
- Book summary by currency (for OI and 25Δ skew computation)
- Historical volatility (realized)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from qt.core.logging import get_logger
from qt.data.base import coerce_utc_index, http_get_json

log = get_logger(__name__)

DERIBIT = "https://www.deribit.com/api/v2"


def fetch_dvol(
    currency: str = "BTC", resolution: str = "60",
    since: datetime | None = None, until: datetime | None = None,
) -> pd.DataFrame:
    """BTC DVOL index OHLC. ``resolution`` in minutes ("60" = 1h)."""

    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=30)
    try:
        data = http_get_json(
            f"{DERIBIT}/public/get_volatility_index_data",
            params={
                "currency": currency,
                "start_timestamp": int(since.timestamp() * 1000),
                "end_timestamp": int(until.timestamp() * 1000),
                "resolution": resolution,
            },
        )
    except Exception as e:
        log.warning("deribit_dvol_failed", error=str(e))
        return pd.DataFrame(columns=["dvol_open", "dvol_high", "dvol_low", "dvol_close"])
    result = (data.get("result") or {}).get("data") or []
    if not result:
        return pd.DataFrame(columns=["dvol_open", "dvol_high", "dvol_low", "dvol_close"])
    df = pd.DataFrame(result, columns=["ts_ms", "dvol_open", "dvol_high", "dvol_low", "dvol_close"])
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    return coerce_utc_index(df.drop(columns=["ts_ms"]))


def fetch_book_summary(currency: str = "BTC") -> pd.DataFrame:
    """Snapshot of every listed BTC option with mark_iv, OI, greeks.

    Use to compute live put/call OI ratio and 25Δ skew. Not historical —
    the snapshot must be polled and stored over time.
    """

    try:
        data = http_get_json(
            f"{DERIBIT}/public/get_book_summary_by_currency",
            params={"currency": currency, "kind": "option"},
        )
    except Exception as e:
        log.warning("deribit_book_summary_failed", error=str(e))
        return pd.DataFrame()
    rows = data.get("result") or []
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def put_call_oi_ratio(book_summary: pd.DataFrame) -> float:
    """Compute current put/call OI ratio from a book-summary snapshot."""

    if book_summary.empty or "instrument_name" not in book_summary.columns:
        return float("nan")
    puts = book_summary[book_summary["instrument_name"].str.endswith("-P")]
    calls = book_summary[book_summary["instrument_name"].str.endswith("-C")]
    put_oi = puts["open_interest"].sum()
    call_oi = calls["open_interest"].sum()
    if call_oi == 0:
        return float("nan")
    return float(put_oi / call_oi)
