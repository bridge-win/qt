"""Closed list of official, keyless spot endpoints. No arbitrary URL overrides."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Venue = Literal["binance", "okx"]
Environment = Literal["production", "testnet", "demo"]


@dataclass(frozen=True)
class SpotSource:
    exchange: Venue
    environment: Environment
    symbol: Literal["BTCUSDT", "BTC-USDT"]
    rest_url: str
    websocket_url: str
    business_websocket_url: str | None = None


_BINANCE_STREAMS = "stream?streams=btcusdt@aggTrade/btcusdt@bookTicker/btcusdt@kline_1m"
SOURCES: dict[tuple[str, str], SpotSource] = {
    ("binance", "production"): SpotSource(
        "binance", "production", "BTCUSDT", "https://data-api.binance.vision/api/v3",
        f"wss://data-stream.binance.vision:443/{_BINANCE_STREAMS}",
    ),
    ("binance", "testnet"): SpotSource(
        "binance", "testnet", "BTCUSDT", "https://testnet.binance.vision/api/v3",
        f"wss://stream.testnet.binance.vision/{_BINANCE_STREAMS}",
    ),
    ("okx", "production"): SpotSource(
        "okx", "production", "BTC-USDT", "https://openapi.okx.com/api/v5",
        "wss://ws.okx.com/ws/v5/public", "wss://ws.okx.com/ws/v5/business",
    ),
    ("okx", "demo"): SpotSource(
        "okx", "demo", "BTC-USDT", "https://openapi.okx.com/api/v5",
        "wss://wspap.okx.com/ws/v5/public", "wss://wspap.okx.com/ws/v5/business",
    ),
}


def spot_source(venue: str, environment: str) -> SpotSource:
    try:
        return SOURCES[venue, environment]
    except KeyError:
        raise ValueError("unsupported spot venue/environment combination") from None
