"""Shared fixtures: synthetic OHLCV with seeded extreme events."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(20251015)


@pytest.fixture
def synthetic_ohlcv(rng: np.random.Generator) -> pd.DataFrame:
    """6 months of hourly BTC-like data with seeded crash events.

    Three crashes are inserted to make sure the composite score has data
    to fire on. Returns a normal DataFrame with UTC index.
    """

    n = 24 * 30 * 6
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    # GBM-ish baseline
    mu = 0.0
    sigma = 0.02
    drift = mu - 0.5 * sigma**2
    shocks = rng.normal(drift, sigma, size=n)

    # Crash events: a couple of -8% to -15% bars.
    crash_idx = [600, 2400, 3700]
    for i in crash_idx:
        shocks[i] += -0.10  # 10% extra drop
        shocks[i + 1] += -0.04
        # snap-back
        shocks[i + 6] += 0.05
        shocks[i + 24] += 0.03

    log_path = np.cumsum(shocks)
    close = 40_000 * np.exp(log_path)
    # Construct OHLC with intra-bar wicks.
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    # Crash bars: make low much lower than close (long lower wick / 插针).
    for i in crash_idx:
        low[i] = close[i] * 0.92
    open_ = np.r_[close[0], close[:-1]]
    volume = rng.lognormal(mean=2.0, sigma=0.5, size=n)
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )
    df.index.name = "ts"
    return df


@pytest.fixture
def crash_indices() -> list[int]:
    return [600, 2400, 3700]
