"""Venue-matched read-only public data; demo liquidity never generates signals."""

import math
import time
from typing import cast

import httpx

from qt.btc_agent.broker import Quote
from qt.btc_agent.signals import Candle

PUBLIC_API = "https://api.binance.com/api/v3"
OKX_PUBLIC_API = "https://openapi.okx.com/api/v5"


def candles() -> list[Candle]:
    response = httpx.get(
        f"{PUBLIC_API}/klines",
        params={"symbol": "BTCUSDT", "interval": "1d", "limit": 1000},
        timeout=20,
    )
    response.raise_for_status()
    return [Candle(int(r[0]), *(float(v) for v in r[1:6])) for r in response.json()]


def quote() -> Quote:
    started = int(time.time() * 1000)
    response = httpx.get(
        f"{PUBLIC_API}/ticker/bookTicker", params={"symbol": "BTCUSDT"}, timeout=20
    )
    response.raise_for_status()
    data = response.json()
    return Quote(float(data["bidPrice"]), float(data["askPrice"]), started)


def _okx_data(path: str, params: dict[str, str | int]) -> list[object]:
    # No credentials or demo header here: strategy signals use actual public prices.
    response = httpx.get(f"{OKX_PUBLIC_API}/{path}", params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("code") != "0" or not isinstance(data.get("data"), list):
        raise ValueError("Invalid OKX public market response")
    return cast(list[object], data["data"])


def okx_candles() -> list[Candle]:
    """Up to 300 confirmed UTC days; enough for the 210-day strategy warmup."""
    rows = _okx_data("market/history-candles", {
        "instId": "BTC-USDT", "bar": "1Dutc", "limit": 300,
    })
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 9 or row[8] not in ("0", "1"):
            raise ValueError("Malformed OKX daily candle")
        if row[8] == "0":
            continue
        # vol is base BTC for SPOT. Other OKX volume columns are quote currency.
        candle = Candle(int(row[0]), *(float(value) for value in row[1:6]))
        if not all(math.isfinite(value) for value in (
            candle.open, candle.high, candle.low, candle.close, candle.volume,
        )):
            raise ValueError("Non-finite OKX daily candle")
        candles.append(candle)
    # The service performs strict UTC alignment, uniqueness, continuity and age checks.
    return sorted(candles, key=lambda candle: candle.open_time)


def okx_quote() -> Quote:
    rows = _okx_data("market/ticker", {"instId": "BTC-USDT"})
    if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("instId") != "BTC-USDT":
        raise ValueError("Invalid OKX public quote identity")
    row = rows[0]
    return Quote(float(row["bidPx"]), float(row["askPx"]), int(row["ts"]))
