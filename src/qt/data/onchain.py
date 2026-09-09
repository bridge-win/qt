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
        data = http_get_json(f"{MEMPOOL}/v1/fees/recommended")
    except Exception as e:
        log.warning("mempool_fees_failed", error=str(e))
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, int | float)}


# --- Derived on-chain series from free Coin Metrics data ----------------

def fetch_coinmetrics_caps(
    since: datetime | None = None,
    until: datetime | None = None,
) -> pd.DataFrame:
    """Daily market cap + realized cap from Coin Metrics community (free).

    Returns columns ``market_cap_usd``, ``realized_cap_usd``. Pulls a
    minimum of 3 years so the 2-year rolling std used by MVRV-Z is warm.
    """

    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=365 * 5)
    since = min(since, until - timedelta(days=365 * 3))
    mc = fetch_coinmetrics("market_cap_usd", since=since, until=until)
    rc = fetch_coinmetrics("realized_cap_usd", since=since, until=until)
    if mc.empty or rc.empty:
        return pd.DataFrame(columns=["market_cap_usd", "realized_cap_usd"])
    return mc.join(rc, how="inner").dropna()


def fetch_coinmetrics_mvrv_z(
    since: datetime | None = None,
    until: datetime | None = None,
    window: int = 365 * 2,
) -> pd.DataFrame:
    """MVRV Z-Score computed from free Coin Metrics caps.

    Replaces the previous (incorrect) use of the raw MVRV ratio as a
    stand-in for the Z-score. Also returns ``nupl`` = (MC-RC)/MC and
    the raw ``mvrv`` ratio for convenience. Rows before the warm-up
    window are dropped.
    """

    from qt.indicators.onchain import mvrv_z_from_caps, nupl_from_caps

    caps = fetch_coinmetrics_caps(since=since, until=until)
    if caps.empty:
        return pd.DataFrame(columns=["mvrv_z", "nupl", "mvrv"])
    out = pd.DataFrame(index=caps.index)
    out["mvrv_z"] = mvrv_z_from_caps(caps["market_cap_usd"], caps["realized_cap_usd"], window=window)
    out["nupl"] = nupl_from_caps(caps["market_cap_usd"], caps["realized_cap_usd"])
    out["mvrv"] = caps["market_cap_usd"] / caps["realized_cap_usd"]
    out = out.dropna(subset=["mvrv_z"])
    if since is not None:
        out = out[out.index >= pd.Timestamp(since)]
    return out


# --- DefiLlama stablecoins (free, no key) ---------------------------------

DEFILLAMA_STABLES = "https://stablecoins.llama.fi/stablecoincharts/all"


def fetch_stablecoin_supply() -> pd.DataFrame:
    """Total stablecoin circulating supply (USD-pegged), daily, from DefiLlama.

    Used for the Stablecoin Supply Ratio (SSR = BTC market cap / stablecoin
    cap): a low SSR means plenty of dry powder relative to BTC.
    Returns column ``stablecoin_cap_usd``.
    """

    try:
        data = http_get_json(DEFILLAMA_STABLES, params={"stablecoin": ""})
    except Exception as e:
        log.warning("defillama_stables_failed", error=str(e))
        return pd.DataFrame(columns=["stablecoin_cap_usd"])
    rows = []
    for item in data or []:
        try:
            ts = int(item["date"])
            total = item.get("totalCirculatingUSD") or {}
            usd = float(total.get("peggedUSD", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({"ts": ts, "stablecoin_cap_usd": usd})
    if not rows:
        return pd.DataFrame(columns=["stablecoin_cap_usd"])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return coerce_utc_index(df)
