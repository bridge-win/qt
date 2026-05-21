"""Sentiment data adapters.

- alternative.me Fear & Greed Index (free, daily, since 2018).
- Santiment GraphQL social volume / sentiment (key in Settings; degrades gracefully).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd

from qt.core.logging import get_logger
from qt.data.base import coerce_utc_index, http_get_json

log = get_logger(__name__)

FNG_API = "https://api.alternative.me/fng/"
SANTIMENT_GQL = "https://api.santiment.net/graphql"


def fetch_fear_greed(limit: int = 0) -> pd.DataFrame:
    """Daily Fear & Greed index. limit=0 means full history."""

    try:
        data = http_get_json(FNG_API, params={"limit": limit, "format": "json"})
    except Exception as e:  # noqa: BLE001
        log.warning("fear_greed_failed", error=str(e))
        return pd.DataFrame(columns=["fear_greed", "fear_greed_label"])
    rows = data.get("data", [])
    if not rows:
        return pd.DataFrame(columns=["fear_greed", "fear_greed_label"])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["timestamp"].astype(int), unit="s", utc=True)
    df["fear_greed"] = df["value"].astype(int)
    df["fear_greed_label"] = df["value_classification"]
    return coerce_utc_index(df[["ts", "fear_greed", "fear_greed_label"]])


def fetch_santiment_social(
    api_key: str,
    metric: str = "sentiment_weighted_total_btc",
    since: datetime | None = None,
    until: datetime | None = None,
) -> pd.DataFrame:
    """Generic Santiment timeseries via getMetric GraphQL.

    Common metrics: ``sentiment_weighted_total_btc``, ``social_volume_total``,
    ``social_dominance_total``.
    """

    if not api_key:
        log.info("santiment_no_key", metric=metric)
        return pd.DataFrame(columns=[metric])
    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=365)

    query = f"""
    {{
      getMetric(metric: "{metric}") {{
        timeseriesData(slug: "bitcoin",
                       from: "{since.isoformat()}",
                       to: "{until.isoformat()}",
                       interval: "1d") {{
          datetime
          value
        }}
      }}
    }}
    """
    try:
        with httpx.Client(timeout=15.0, headers={"Authorization": f"Apikey {api_key}"}) as c:
            r = c.post(SANTIMENT_GQL, json={"query": query})
            r.raise_for_status()
            payload = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("santiment_failed", metric=metric, error=str(e))
        return pd.DataFrame(columns=[metric])

    rows = (payload.get("data") or {}).get("getMetric", {}).get("timeseriesData", [])
    if not rows:
        return pd.DataFrame(columns=[metric])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["datetime"], utc=True)
    df[metric] = pd.to_numeric(df["value"], errors="coerce")
    return coerce_utc_index(df[["ts", metric]])
