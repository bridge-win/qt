"""Smart money + event detector tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qt.indicators.events import (
    flash_crash,
    funding_flush,
    liquidation_cascade,
    oi_unwind,
    wick_cluster,
)
from qt.indicators.smartmoney import (
    coinbase_premium_extreme,
    coinbase_premium_index,
    ssr_oscillator,
    stablecoin_supply_ratio,
    whale_net_z,
    whale_ratio_z,
)


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")


def test_coinbase_premium_negative_extreme() -> None:
    idx = _idx(20)
    cb = pd.Series(40_000.0, index=idx)
    bn = pd.Series(40_050.0, index=idx)  # Binance above Coinbase -> negative premium
    prem = coinbase_premium_index(cb, bn)
    ext = coinbase_premium_extreme(prem, sustained_bars=4, threshold=-0.0005)
    assert ext.iloc[-1]


def test_ssr_oscillator_sign() -> None:
    n = 250
    idx = _idx(n)
    # SSR rising over time -> latest z should be > 0
    mcap = pd.Series(np.linspace(100, 200, n), index=idx)
    stable = pd.Series(np.linspace(20, 20.5, n), index=idx)
    ssr = stablecoin_supply_ratio(mcap, stable)
    z = ssr_oscillator(ssr, window=200)
    assert z.dropna().iloc[-1] > 0


def test_whale_ratio_z_drops_on_normalization() -> None:
    n = 30
    idx = _idx(n)
    # Stable whale ratio then a single drop -> deep negative Z at the dip bar.
    wr = pd.Series([0.8] * 29 + [0.3], index=idx)
    z = whale_ratio_z(wr, window=20)
    assert z.dropna().iloc[-1] < -1


def test_whale_net_z_extreme() -> None:
    n = 30
    idx = _idx(n)
    net = pd.Series([0.0] * 29 + [-50_000.0], index=idx)
    z = whale_net_z(net, window=20)
    assert z.dropna().iloc[-1] < -1


def test_liquidation_cascade_fires_on_notional() -> None:
    idx = _idx(48)
    liq = pd.Series([1e6] * 24 + [5e7] * 24, index=idx)  # 24 * $50M = $1.2B in 24h
    out = liquidation_cascade(liq, bars_24h=24, notional_threshold=4e8, z_threshold=3.0)
    assert out.iloc[-1]


def test_flash_crash_detects_drop() -> None:
    idx = _idx(20)
    p = np.r_[np.full(15, 100.0), np.linspace(100, 90, 5)]  # -10% in 5 bars
    out = flash_crash(pd.Series(p, index=idx), threshold_pct=0.08, bars=4)
    assert out.iloc[-1]


def test_wick_cluster_counts() -> None:
    idx = _idx(10)
    wk = pd.Series([0.5, 4.0, 0.2, 3.5, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0], index=idx)
    out = wick_cluster(wk, window=6, count=2, ratio_min=3.0)
    # At index 3 we have wick at idx 1 (4.0) and idx 3 (3.5) within last 6 bars.
    assert bool(out.iloc[3])
    assert not bool(out.iloc[-1])


def test_oi_unwind_drop() -> None:
    idx = _idx(48)
    oi = pd.Series(np.r_[np.full(24, 100.0), np.full(24, 80.0)], index=idx)
    out = oi_unwind(oi, bars_24h=24, drop_pct=0.10)
    assert out.iloc[-1]


def test_funding_flush_sustained() -> None:
    idx = _idx(20)
    fr = pd.Series([-0.0005] * 10 + [0.0001] * 10, index=idx)
    out = funding_flush(fr, window=9, threshold=-0.0001)
    # Window of 9 negatives within first 10 bars -> True at bar idx 8.
    assert out.iloc[8]
    assert not out.iloc[-1]
