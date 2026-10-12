"""Read-only production market data; simulated liquidity never generates signals."""

import time

import httpx

from qt.btc_agent.broker import Quote
from qt.btc_agent.signals import Candle

PUBLIC_API = "https://api.binance.com/api/v3"


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
