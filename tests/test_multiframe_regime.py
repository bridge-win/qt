"""Multi-timeframe and regime detection tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qt.indicators.options import (
    backwardation,
    dvol_extreme,
    put_call_oi_extreme,
    put_skew_extreme,
)
from qt.indicators.regime import hurst_dfa, regime_label, vol_regime_z
from qt.signal.multiframe import multitf_confirm


def test_multitf_confirm_requires_all() -> None:
    n = 24 * 30
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    # Sharply declining price -> RSI oversold across timeframes
    close = pd.Series(np.linspace(50_000, 30_000, n), index=idx)
    out = multitf_confirm(close, rsi_max_1h=40, rsi_max_4h=40, rsi_max_1d=50)
    assert out.iloc[-1]


def test_multitf_confirm_blocks_when_one_fails() -> None:
    n = 24 * 30
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    close = pd.Series(np.linspace(30_000, 50_000, n), index=idx)  # rising
    out = multitf_confirm(close)
    assert not out.iloc[-1]


def test_hurst_in_valid_range() -> None:
    n = 24 * 60
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(0)
    close = 40_000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    h = hurst_dfa(pd.Series(close, index=idx), window=24 * 30)
    valid = h.dropna()
    assert (valid > 0.1).all() and (valid < 1.5).all()


def test_vol_regime_z_responds_to_shock() -> None:
    n = 24 * 90
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(1)
    base = rng.normal(0, 0.005, n)
    base[-100:] = rng.normal(0, 0.03, 100)  # 6x vol regime change
    close = pd.Series(40_000 * np.exp(np.cumsum(base)), index=idx)
    z = vol_regime_z(close, window=24 * 30)
    assert z.dropna().iloc[-1] > 1.0


def test_regime_label_strings() -> None:
    n = 24 * 60
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(0)
    close = pd.Series(40_000 * np.exp(np.cumsum(rng.normal(0, 0.005, n))), index=idx)
    lbl = regime_label(close, hurst_window=24 * 30, vol_window=24 * 30)
    assert lbl.dropna().isin(
        ["meanrevert_lowvol", "trend_lowvol", "meanrevert_highvol", "trend_highvol"]
    ).all()


def test_dvol_extreme() -> None:
    s = pd.Series([50, 60, 95, 110, 80])
    out = dvol_extreme(s, threshold=95.0)
    assert list(out) == [False, False, True, True, False]


def test_put_skew_extreme() -> None:
    s = pd.Series([5.0, 10.0, 16.0, 20.0])
    out = put_skew_extreme(s, threshold=15.0)
    assert list(out) == [False, False, True, True]


def test_put_call_oi_extreme_uses_smoothing() -> None:
    s = pd.Series([0.5, 0.9, 1.1, 1.2, 1.3])
    out = put_call_oi_extreme(s, threshold=1.0, smoothing=2)
    # After smoothing the latest value should exceed threshold
    assert bool(out.iloc[-1])


def test_backwardation_threshold() -> None:
    s = pd.Series([-0.01, 0.0, 0.02])
    out = backwardation(s, threshold=0.0)
    assert list(out) == [True, True, False]
