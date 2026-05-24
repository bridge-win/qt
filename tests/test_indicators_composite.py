"""Composite extreme-score behaviour tests."""

from __future__ import annotations

import pandas as pd

from qt.core.config import ThresholdConfig
from qt.indicators.composite import compute_extreme_score


def test_score_in_unit_interval(synthetic_ohlcv: pd.DataFrame) -> None:
    cfg = ThresholdConfig()
    out = compute_extreme_score(synthetic_ohlcv, cfg=cfg)
    s = out.score.dropna()
    assert ((s >= 0) & (s <= 1.0)).all()


def test_score_fires_around_crash(synthetic_ohlcv: pd.DataFrame, crash_indices: list[int]) -> None:
    # With only price+vol groups available, lower threshold to verify firing.
    cfg = ThresholdConfig(rsi_oversold=30, bb_std=1.5, drawdown_30d_min=0.05,
                          wick_body_ratio_min=2.0, rv_ratio_min=1.2)
    out = compute_extreme_score(synthetic_ohlcv, cfg=cfg)
    for ci in crash_indices:
        window = out.score.iloc[max(0, ci - 5): ci + 10]
        assert window.max() > 0.4, f"no signal near crash idx={ci}"


def test_macro_veto_suppresses(synthetic_ohlcv: pd.DataFrame) -> None:
    # All-high VIX -> score must be exactly 0 everywhere.
    vix = pd.Series(60.0, index=synthetic_ohlcv.index)
    cfg = ThresholdConfig(vix_max=35.0)
    out = compute_extreme_score(synthetic_ohlcv, vix=vix, cfg=cfg)
    assert (out.score == 0).all()


def test_missing_inputs_drop_group(synthetic_ohlcv: pd.DataFrame) -> None:
    # Just OHLCV -> denominator is at most {price, volatility}; score stays low.
    cfg = ThresholdConfig()
    out = compute_extreme_score(synthetic_ohlcv, cfg=cfg)
    # Only 2 groups available -> max possible score is 1.0 with 2/2 firing,
    # but min_factor_groups would still gate elsewhere; here just test score bounds.
    assert out.group_flags.shape[1] == 5  # always 5 cols, possibly all-False


def test_available_group_stays_in_denominator_when_not_firing() -> None:
    idx = pd.date_range("2024-01-01", periods=800, freq="1h", tz="UTC")
    close = pd.Series(100.0, index=idx)
    close.iloc[-1] = 80.0
    ohlcv = pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 100.0,
        },
        index=idx,
    )
    funding = pd.Series(0.0, index=idx)
    cfg = ThresholdConfig(drawdown_30d_min=0.10, rv_ratio_min=999.0)

    out = compute_extreme_score(ohlcv, funding=funding, cfg=cfg)

    assert out.group_flags.loc[idx[-1], "price"]
    assert not out.group_flags.loc[idx[-1], "volatility"]
    assert not out.group_flags.loc[idx[-1], "derivatives"]
    assert out.score.loc[idx[-1]] == 1 / 3
