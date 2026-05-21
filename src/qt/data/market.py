"""Spot OHLCV ingestion via ccxt (Binance / OKX / Bybit / Coinbase / Kraken)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from qt.core.logging import get_logger
from qt.data.base import empty_ohlcv

log = get_logger(__name__)

try:
    import ccxt  # type: ignore
except ImportError:  # ccxt may not be installed in minimal envs
    ccxt = None  # type: ignore


_TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def fetch_ohlcv(
    exchange_id: str = "binance",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    since: datetime | None = None,
    until: datetime | None = None,
    limit_per_call: int = 1000,
) -> pd.DataFrame:
    """Page through ccxt fetch_ohlcv until `until`. Returns UTC-indexed OHLCV."""

    if ccxt is None:
        log.warning("ccxt_not_installed", returning="empty")
        return empty_ohlcv()

    klass = getattr(ccxt, exchange_id, None)
    if klass is None:
        raise ValueError(f"Unknown ccxt exchange: {exchange_id}")
    ex = klass({"enableRateLimit": True})
    if not ex.has.get("fetchOHLCV"):
        raise RuntimeError(f"{exchange_id} does not support OHLCV via ccxt")

    if until is None:
        until = datetime.now(tz=timezone.utc)
    if since is None:
        since = until - timedelta(days=365)
    since_ms = int(since.timestamp() * 1000)
    until_ms = int(until.timestamp() * 1000)
    step = _TF_MS.get(timeframe)
    if step is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    rows: list[list[float]] = []
    cursor = since_ms
    while cursor < until_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=limit_per_call)
        except Exception as e:  # noqa: BLE001
            log.warning("ohlcv_batch_failed", exchange=exchange_id, error=str(e), cursor=cursor)
            break
        if not batch:
            break
        rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= cursor:
            break
        cursor = last_ts + step

    if not rows:
        return empty_ohlcv()
    df = pd.DataFrame(rows, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="ts_ms").sort_values("ts_ms")
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.drop(columns="ts_ms").set_index("ts")
    df = df[df.index <= pd.Timestamp(until, tz="UTC")]
    return df.astype({"open": "float64", "high": "float64", "low": "float64",
                      "close": "float64", "volume": "float64"})
