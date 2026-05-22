"""On-chain BTC data adapters.

Free sources (no key required):
- Coin Metrics community API: ~400 metrics including MVRV, SOPR proxies,
  realized cap, active addresses. Daily granularity from 2009.
- mempool.space: live mempool, fees.
- blockchain.info: high-level chain stats.

Paid sources (key passed through Settings if present):
- Glassnode: MVRV-Z, SOPR variants, NUPL, exchange flows, LTH metrics.
- CryptoQuant: exchange netflows, miner data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from qt.core.logging import get_logger
from qt.data.base import coerce_utc_index, http_get_json

log = get_logger(__name__)

COINMETRICS = "https://community-api.coinmetrics.io/v4"
GLASSNODE = "https://api.glassnode.com/v1/metrics"


# --- Coin Metrics (free) --------------------------------------------------

# Mapping of QT canonical metric name -> Coin Metrics community metric id.
COINMETRICS_METRICS = {
    "price_usd": "PriceUSD",
    "realized_cap_usd": "CapRealUSD",
    "market_cap_usd": "CapMrktCurUSD",
    "mvrv": "CapMVRVCur",
    "active_addr": "AdrActCnt",
    "hashrate": "HashRate",
    "tx_count": "TxCnt",
    "tx_volume_usd": "TxTfrValAdjUSD",
}


def fetch_coinmetrics(
    metric: str,
    asset: str = "btc",
    since: datetime | None = None,
    until: datetime | None = None,
) -> pd.DataFrame:
    """Fetch a single daily metric from Coin Metrics community API."""

    cm_id = COINMETRICS_METRICS.get(metric, metric)
    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=365 * 5)

    rows: list[dict[str, str]] = []
    page_token: str | None = None
    while True:
        params: dict[str, object] = {
            "assets": asset,
            "metrics": cm_id,
            "start_time": since.strftime("%Y-%m-%d"),
            "end_time": until.strftime("%Y-%m-%d"),
            "page_size": 10000,
        }
        if page_token:
            params["next_page_token"] = page_token
        try:
            data = http_get_json(f"{COINMETRICS}/timeseries/asset-metrics", params=params)
        except Exception as e:
            log.warning("coinmetrics_fetch_failed", metric=metric, error=str(e))
            break
        rows.extend(data.get("data", []))
        page_token = data.get("next_page_token")
        if not page_token:
            break

    if not rows:
        return pd.DataFrame(columns=[metric])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["time"], utc=True)
    df[metric] = pd.to_numeric(df[cm_id], errors="coerce")
    return coerce_utc_index(df[["ts", metric]])


# --- Glassnode (paid; degrades when key missing) -------------------------

GLASSNODE_METRICS = {
    "mvrv_z": "/market/mvrv_z_score",
    "sopr": "/indicators/sopr",
    "sopr_adj": "/indicators/sopr_adjusted",
    "lth_sopr": "/indicators/sopr_lth",
    "nupl": "/indicators/net_unrealized_profit_loss",
    "exchange_netflow": "/transactions/transfers_volume_exchanges_net",
    "puell_multiple": "/indicators/puell_multiple",
    "reserve_risk": "/indicators/reserve_risk",
}


def fetch_glassnode(
    metric: str,
    api_key: str,
    asset: str = "BTC",
    since: datetime | None = None,
    until: datetime | None = None,
    resolution: str = "24h",
) -> pd.DataFrame:
    """Single Glassnode metric. Returns empty if no api_key."""

    if not api_key:
        log.info("glassnode_no_key", metric=metric)
        return pd.DataFrame(columns=[metric])
    path = GLASSNODE_METRICS.get(metric)
    if not path:
        raise ValueError(f"Unknown Glassnode metric: {metric}")
    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=365 * 5)

    try:
        data = http_get_json(
            f"{GLASSNODE}{path}",
            params={
                "a": asset,
                "s": int(since.timestamp()),
                "u": int(until.timestamp()),
                "i": resolution,
                "api_key": api_key,
            },
        )
    except Exception as e:
        log.warning("glassnode_fetch_failed", metric=metric, error=str(e))
        return pd.DataFrame(columns=[metric])

    if not data:
        return pd.DataFrame(columns=[metric])
    df = pd.DataFrame(data)
    df["ts"] = pd.to_datetime(df["t"], unit="s", utc=True)
    df[metric] = pd.to_numeric(df["v"], errors="coerce")
    return coerce_utc_index(df[["ts", metric]])


# --- mempool.space (free) ------------------------------------------------

MEMPOOL = "https://mempool.space/api"


def fetch_mempool_fees() -> dict[str, float]:
    """Live recommended fee rates (sat/vB). Useful as a network-stress signal."""

    try:
        return http_get_json(f"{MEMPOOL}/v1/fees/recommended")
    except Exception as e:
        log.warning("mempool_fees_failed", error=str(e))
        return {}
