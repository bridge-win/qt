"""Conservative daily BTC hypotheses, evaluated only on completed UTC bars.

Thresholds are research defaults, not fitted estimates of future profitability.
Price distress and a stress event must coincide on one of the last three bars;
the newest closed bar must then show a positive close-to-close recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Literal

import pandas as pd

from qt.indicators.price import atr, rsi

DAY_MS = 86_400_000
MIN_CANDLES = 210
MAX_AGE_MS = 36 * 60 * 60 * 1000
_EPSILON = 1e-12


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    action: Literal["buy", "sell", "hold"]
    reason: str
    bar_time: int
    price: float
    atr: float
    factors: dict[str, float | bool]


def _completed_candles(candles: list[Candle], now_ms: int) -> list[Candle]:
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a nonnegative integer timestamp")

    completed: list[Candle] = []
    previous_time: int | None = None
    for candle in candles:
        timestamp = candle.open_time
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
            or timestamp % DAY_MS != 0
        ):
            raise ValueError("Candle open_time must align to a UTC daily boundary")
        if timestamp > now_ms:
            raise ValueError("Candle open_time is in the future")
        if previous_time is not None and timestamp != previous_time + DAY_MS:
            raise ValueError("Candles must be ordered, unique, and contiguous daily bars")
        previous_time = timestamp
        for name in ("open", "high", "low", "close", "volume"):
            value = getattr(candle, name)
            if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
                raise ValueError(f"Candle {name} must be a finite number")
            if value < 0 or (name != "volume" and value == 0):
                raise ValueError("Candle prices must be positive and volume nonnegative")
        if candle.low > min(candle.open, candle.close) or candle.high < max(
            candle.open, candle.close
        ):
            raise ValueError("Candle high and low must contain its open and close")
        if timestamp + DAY_MS <= now_ms:
            completed.append(candle)

    if len(completed) < MIN_CANDLES:
        raise ValueError(f"At least {MIN_CANDLES} completed daily candles are required")
    if now_ms - (completed[-1].open_time + DAY_MS) > MAX_AGE_MS:
        raise ValueError("Latest completed daily candle is stale (older than 36 hours)")
    return completed


def evaluate(candles: list[Candle], now_ms: int, *, has_position: bool = False) -> Signal:
    """Evaluate a spot long-only candidate; malformed or stale input fails closed.

    An unfinished current-day candle is validated but never used in indicators.
    At most one open position is assumed; sizing and execution belong to the
    caller. A hold is also returned for an entry candidate while already long.
    """

    completed = _completed_candles(candles, now_ms)
    close = pd.Series([c.close for c in completed], dtype="float64")
    high = pd.Series([c.high for c in completed], dtype="float64")
    low = pd.Series([c.low for c in completed], dtype="float64")
    volume = pd.Series([c.volume for c in completed], dtype="float64")

    # Wilder RSI has an undefined 0/0 for an entirely flat market: neutral 50.
    strength = rsi(close, period=14).fillna(50.0)
    volatility = float(atr(high, low, close, period=14).iloc[-1])
    peak = close.rolling(90).max()
    drawdown = (close - peak) / peak
    daily_return = close.diff() / close.shift(1)
    prior_volume = volume.shift(1).rolling(30).mean()
    # A zero historical volume cannot establish a meaningful volume multiple.
    volume_ratio = (volume / prior_volume.where(prior_volume > 0)).fillna(0.0)
    sma_ratio = close / close.rolling(200).mean()

    distressed = (strength <= 25 + _EPSILON) & (drawdown <= -0.30 + _EPSILON)
    stressed = (volume_ratio >= 2.0 - _EPSILON) | (daily_return <= -0.08 + _EPSILON)
    capitulation_recent = bool((distressed & stressed).iloc[-3:].any())
    stabilized = bool(close.iloc[-1] > close.iloc[-2])
    overheat = bool(strength.iloc[-1] >= 80 - _EPSILON and sma_ratio.iloc[-1] >= 1.5 - _EPSILON)

    factors: dict[str, float | bool] = {
        "rsi14": float(strength.iloc[-1]),
        "drawdown90": float(drawdown.iloc[-1]),
        "volume_ratio30": float(volume_ratio.iloc[-1]),
        "daily_return": float(daily_return.iloc[-1]),
        "sma200_ratio": float(sma_ratio.iloc[-1]),
        "price_distress": bool(distressed.iloc[-1]),
        "market_stress": bool(stressed.iloc[-1]),
        "capitulation_recent": capitulation_recent,
        "stabilized": stabilized,
        "overheat": overheat,
    }
    if not isfinite(volatility) or any(not isfinite(value) for value in factors.values()):
        raise ValueError("Computed indicators must be finite")

    action: Literal["buy", "sell", "hold"] = "hold"
    reason = "No completed daily extreme satisfies all entry or exit conditions."
    if has_position and overheat:
        action = "sell"
        reason = "Daily RSI is at least 80 and price is at least 1.5 times SMA200."
    elif not has_position and capitulation_recent and stabilized:
        action = "buy"
        reason = (
            "Price distress and stress coincided within three days; the latest close recovered."
        )
    elif has_position and capitulation_recent and stabilized:
        reason = "Recovery candidate detected, but a position is already open."
    elif capitulation_recent:
        reason = "Recent capitulation requires a higher completed daily close before entry."

    latest = completed[-1]
    return Signal(action, reason, latest.open_time, latest.close, volatility, factors)
