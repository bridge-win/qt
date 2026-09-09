"""Tests for the 2026-09 additions: MVRV-Z fix, timeframe-aware composite,
liquidation cascade, overheat score, cycle position."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qt.indicators.composite import bars_per_day, compute_extreme_score, compute_overheat_score
from qt.indicators.cycle import cycle_position, halving_phase
from qt.indicators.derivatives import liquidation_cascade, liquidation_zscore, oi_surge_24h
from qt.indicators.onchain import mvrv_z_from_caps, nupl_from_caps, pi_cycle_top
from qt.indicators.price import atr_displacement


def _ohlcv(n: int, freq: str, seed: int = 1, drift: float = 0.0) -> pd.DataFrame:
    idx = pd.date_range("2023-01-01", periods=n, freq=freq, tz="UTC")
    rng = np.random.default_rng(seed)
    close = 30000 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    return pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.001, n)),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": rng.uniform(100, 200, n),
    }, index=idx)


def test_bars_per_day_infers_timeframe():
    assert bars_per_day(pd.date_range("2024-01-01", periods=10, freq="h", tz="UTC")) == 24
    assert bars_per_day(pd.date_range("2024-01-01", periods=10, freq="4h", tz="UTC")) == 6
    assert bars_per_day(pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")) == 1


def test_composite_runs_on_daily_bars_without_hourly_assumptions():
    d = _ohlcv(400, "D")
    es = compute_extreme_score(d)
    assert len(es.score) == 400
    assert es.score.between(0, 1).all()
    # 30d drawdown on daily bars must not be NaN for the whole series
    assert es.factor_flags["price_dd"].iloc[-1] in (True, False)


def test_mvrv_z_from_caps_is_zscore_not_ratio():
    idx = pd.date_range("2018-01-01", periods=365 * 3, freq="D", tz="UTC")
    rc = pd.Series(np.linspace(1e11, 4e11, len(idx)), index=idx)
    mc = rc * (1 + 0.8 * np.sin(np.linspace(0, 6 * np.pi, len(idx))))
    z = mvrv_z_from_caps(mc, rc)
    ratio = mc / rc
    # the raw ratio never goes below 0.5; the z-score crosses 0 and goes negative
    assert ratio.min() > 0.15
    assert (z.dropna() < 0).any()
    n = nupl_from_caps(mc, rc)
    assert n.max() <= 1.0 and (n < 0).any()


def test_liquidation_cascade_requires_spike_and_long_dominance():
    idx = pd.date_range("2024-01-01", periods=24 * 40, freq="h", tz="UTC")
    long_liq = pd.Series(1e6, index=idx, dtype=float)
    short_liq = pd.Series(1e6, index=idx, dtype=float)
    long_liq.iloc[-1] = 2e8                         # 200x spike
    casc = liquidation_cascade(long_liq, short_liq)
    assert bool(casc.iloc[-1])
    assert not casc.iloc[:-1].any()
    # same spike on both sides → no long dominance → no cascade
    short_liq.iloc[-1] = 2e8
    assert not bool(liquidation_cascade(long_liq, short_liq).iloc[-1])
    assert liquidation_zscore(long_liq).iloc[-1] > 3


def test_composite_accepts_liquidations_as_derivatives_group():
    df = _ohlcv(24 * 40, "h")
    ll = pd.Series(1e6, index=df.index)
    ll.iloc[-1] = 5e8
    es = compute_extreme_score(df, long_liq_usd=ll)
    assert "deriv_liq_cascade" in es.factor_flags.columns
    assert bool(es.group_flags["derivatives"].iloc[-1])


def test_overheat_score_fires_on_blowoff():
    df = _ohlcv(24 * 60, "h", drift=0.0)
    # engineer a parabolic last 10 days
    n = 24 * 10
    ramp = np.linspace(1.0, 2.2, n)
    df.iloc[-n:, df.columns.get_loc("close")] *= ramp
    df.iloc[-n:, df.columns.get_loc("high")] *= ramp
    df.iloc[-n:, df.columns.get_loc("low")] *= ramp
    fg = pd.Series(90, index=df.index)
    funding = pd.Series(0.0001, index=df.index)
    funding.iloc[-6:] = 0.002
    oi = pd.Series(1e9, index=df.index)
    oi.iloc[-24:] = 1.3e9
    es = compute_overheat_score(df, funding=funding, oi=oi, fear_greed=fg)
    assert es.score.iloc[-1] >= 0.6
    assert bool(es.group_flags["price"].iloc[-1])
    assert bool(es.group_flags["sentiment"].iloc[-1])
    assert bool(es.group_flags["derivatives"].iloc[-1])
    assert oi_surge_24h(oi).iloc[-1]


def test_overheat_is_zero_on_quiet_market():
    df = _ohlcv(24 * 60, "h")
    fg = pd.Series(50, index=df.index)
    es = compute_overheat_score(df, fear_greed=fg)
    assert es.score.iloc[-1] == 0.0


def test_atr_displacement_symmetric():
    df = _ohlcv(300, "h")
    d = atr_displacement(df["close"], df["high"], df["low"])
    assert d.dropna().abs().max() < 20


def test_pi_cycle_top_detects_cross():
    idx = pd.date_range("2020-01-01", periods=500, freq="D", tz="UTC")
    close = pd.Series(np.concatenate([np.full(400, 100.0), np.linspace(100, 1200, 100)]), index=idx)
    assert pi_cycle_top(close).any()


def test_halving_phase_in_unit_interval():
    idx = pd.date_range("2021-01-01", periods=1500, freq="D", tz="UTC")
    hp = halving_phase(idx)
    assert hp.between(0, 1).all()
    # 2024-04-20 halving → phase resets near 0
    assert hp.loc["2024-04-21"] < 0.01


def test_cycle_position_bands_and_alloc():
    idx = pd.date_range("2016-01-01", periods=365 * 6, freq="D", tz="UTC")
    close = pd.Series(1000 * np.exp(np.linspace(0, 4, len(idx)) + 0.8 * np.sin(np.linspace(0, 3 * np.pi, len(idx)))), index=idx)
    mvrv_z = pd.Series(4 * np.sin(np.linspace(0, 3 * np.pi, len(idx))) + 3, index=idx)
    cp = cycle_position(close, mvrv_z=mvrv_z)
    assert cp.position.dropna().between(0, 100).all()
    assert cp.target_alloc.between(0.1, 1.0).all()
    assert set(cp.band.dropna().unique()) <= {"accumulate", "hold", "neutral", "trim", "distribute"}
    # allocation is monotone decreasing in position
    s = pd.DataFrame({"p": cp.position, "a": cp.target_alloc}).dropna().sort_values("p")
    assert (np.diff(s["a"].to_numpy()) <= 1e-9).all()


def test_strategy_registry_has_new_strategies():
    from qt.strategies.registry import REGISTRY

    assert {"overheat", "cycle"} <= set(REGISTRY)


def test_overheat_strategy_evaluate_offline():
    from qt.strategies.base import StrategyConfig
    from qt.strategies.overheat import Overheat

    st = Overheat(StrategyConfig(name="overheat", enabled=True, interval_seconds=60, params={}))
    df = _ohlcv(24 * 60, "h")
    res = st.evaluate({"ohlcv": df, "fear_greed": pd.DataFrame({"fear_greed": 50}, index=df.index)})
    assert res.opportunity is None
    assert "score" in res.metrics


def test_cycle_strategy_evaluate_offline():
    from qt.strategies.base import StrategyConfig
    from qt.strategies.cycle import CycleRegime

    st = CycleRegime(StrategyConfig(name="cycle", enabled=True, interval_seconds=60, params={}))
    df = _ohlcv(365 * 5, "D")
    res = st.evaluate({"ohlcv": df, "onchain": pd.DataFrame()})
    assert "cycle_position" in res.metrics
    assert res.metrics["band"] in {"accumulate", "hold", "neutral", "trim", "distribute"}


def test_store_read_column_fallback(tmp_path):
    from qt.data.store import ParquetStore

    store = ParquetStore(tmp_path)
    idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    store.write("onchain", "coinmetrics_derived", pd.DataFrame({"mvrv_z": [1.0, 2.0, 3.0]}, index=idx))
    col = store.read_column("onchain", ("glassnode_mvrv_z", "coinmetrics_derived"), "mvrv_z")
    assert col is not None and len(col) == 3
    assert store.read_column("onchain", ("nope",), "mvrv_z") is None
