"""Unit tests for price-action indicators."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qt.indicators.price import (
    atr,
    bollinger_bands,
    bollinger_zscore,
    drawdown_from_high,
    rsi,
    wick_ratio,
)


def test_rsi_extremes() -> None:
    # Monotonic up -> RSI saturates near 100
    close = pd.Series(np.linspace(100, 200, 50))
    out = rsi(close, period=14)
    assert out.dropna().iloc[-1] > 90

    # Monotonic down -> RSI near 0
    close = pd.Series(np.linspace(200, 100, 50))
    out = rsi(close, period=14)
    assert out.dropna().iloc[-1] < 10


def test_bollinger_bands_sanity() -> None:
    close = pd.Series(np.linspace(100, 200, 100))
    bb = bollinger_bands(close, period=20, n_std=2.0)
    assert {"bb_mid", "bb_upper", "bb_lower", "bb_width"} == set(bb.columns)
    # Upper above mid above lower
    last = bb.dropna().iloc[-1]
    assert last["bb_upper"] > last["bb_mid"] > last["bb_lower"]


def test_bollinger_zscore_negative_on_crash() -> None:
    arr = np.r_[np.linspace(100, 100, 30), np.linspace(100, 80, 10)]
    z = bollinger_zscore(pd.Series(arr), period=20)
    assert z.dropna().iloc[-1] < -1


def test_atr_positive() -> None:
    n = 50
    high = pd.Series(np.linspace(100, 200, n)) + 5
    low = pd.Series(np.linspace(100, 200, n)) - 5
    close = pd.Series(np.linspace(100, 200, n))
    a = atr(high, low, close, period=14)
    assert (a.dropna() > 0).all()


def test_drawdown_negative_after_crash() -> None:
    close = pd.Series([100] * 50 + [70] * 5)
    dd = drawdown_from_high(close, window=40)
    assert dd.iloc[-1] < -0.25


def test_wick_ratio_long_lower_shadow() -> None:
    o = pd.Series([100.0])
    c = pd.Series([99.5])
    h = pd.Series([100.5])
    low = pd.Series([90.0])  # long lower wick
    wr = wick_ratio(o, h, low, c)
    # body=0.5, lower wick = min(o,c)-low = 99.5-90 = 9.5 -> ratio ~19
    assert wr.iloc[0] > 5
