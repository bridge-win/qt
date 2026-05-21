"""News adapters: CryptoPanic (votes/labels) + GDELT (academic, structured).

For research, GDELT 2.0 GKG returns rich tone/themes signals that can be
aggregated into a daily news-stress index.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from qt.core.logging import get_logger
from qt.data.base import coerce_utc_index, http_get_json

log = get_logger(__name__)

CRYPTOPANIC = "https://cryptopanic.com/api/v1/posts/"


def fetch_cryptopanic(
    token: str,
    currencies: str = "BTC",
    filter_: str = "rising",
    public: bool = True,
) -> pd.DataFrame:
    """Latest CryptoPanic posts. Returns df with sentiment vote columns."""

    if not token:
        log.info("cryptopanic_no_token")
        return pd.DataFrame()
    try:
        data = http_get_json(
            CRYPTOPANIC,
            params={
                "auth_token": token,
                "currencies": currencies,
                "filter": filter_,
                "public": "true" if public else "false",
            },
        )
    except Exception as e:  # noqa: BLE001
        log.warning("cryptopanic_failed", error=str(e))
        return pd.DataFrame()
    rows = data.get("results", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["created_at"], utc=True)
    votes = pd.json_normalize(df["votes"])  # type: ignore[arg-type]
    df = pd.concat([df[["ts", "title", "url"]], votes.add_prefix("votes_")], axis=1)
    return coerce_utc_index(df)


def aggregate_news_sentiment(news_df: pd.DataFrame) -> pd.Series:
    """Naive net-positive ratio per day. Replace with NLP model in production."""

    if news_df.empty:
        return pd.Series(dtype="float64", name="news_sentiment")
    pos = news_df.get("votes_positive", pd.Series(0, index=news_df.index)).fillna(0)
    neg = news_df.get("votes_negative", pd.Series(0, index=news_df.index)).fillna(0)
    daily_pos = pos.resample("1D").sum()
    daily_neg = neg.resample("1D").sum()
    denom = (daily_pos + daily_neg).replace(0, pd.NA)
    s = ((daily_pos - daily_neg) / denom).astype("float64").rename("news_sentiment")
    return s.dropna()


def fetch_gdelt_btc_tone(timespan: str = "30d") -> pd.DataFrame:
    """GDELT Doc API: BTC tone time series. Public, no key.

    `timespan` is a GDELT shortcut, e.g. ``7d``, ``30d``, ``3months``.
    """

    try:
        data = http_get_json(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": "bitcoin OR BTC",
                "mode": "TimelineTone",
                "format": "json",
                "timespan": timespan,
            },
        )
    except Exception as e:  # noqa: BLE001
        log.warning("gdelt_failed", error=str(e))
        return pd.DataFrame(columns=["news_tone"])
    series = (data.get("timeline") or [{}])[0].get("data", [])
    if not series:
        return pd.DataFrame(columns=["news_tone"])
    df = pd.DataFrame(series)
    df["ts"] = pd.to_datetime(df["date"], utc=True)
    df["news_tone"] = pd.to_numeric(df["value"], errors="coerce")
    return coerce_utc_index(df[["ts", "news_tone"]])
