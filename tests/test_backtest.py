"""End-to-end backtest smoke test on synthetic OHLCV."""

from __future__ import annotations

import pandas as pd
import pytest

from qt.backtest.engine import Backtester
from qt.backtest.validation import ohlcv_fingerprint
from qt.core.config import RiskConfig, ThresholdConfig


def test_backtest_runs_on_synthetic(synthetic_ohlcv: pd.DataFrame) -> None:
    # Permissive thresholds so we get some trades within the 6-month window.
    th = ThresholdConfig(
        rsi_oversold=35, bb_std=1.5, drawdown_30d_min=0.05,
        wick_body_ratio_min=2.0, rv_ratio_min=1.3,
        entry_score_min=0.4, min_factor_groups=1,
        vix_max=999, dxy_z_max=999,
    )
    fg = pd.Series(8, index=synthetic_ohlcv.index)  # always extreme fear

    bt = Backtester(thresholds=th, risk_cfg=RiskConfig(), initial_cash=10_000)
    result = bt.run(ohlcv=synthetic_ohlcv, fear_greed=fg)

    assert len(result.equity_curve) == len(synthetic_ohlcv)
    # Sanity: equity always positive
    assert (result.equity_curve > 0).all()
    # We expect at least one trade with the permissive config
    assert result.metrics.num_trades >= 1


def test_backtest_no_trades_with_strict_config(synthetic_ohlcv: pd.DataFrame) -> None:
    th = ThresholdConfig(entry_score_min=0.999, min_factor_groups=5)
    bt = Backtester(thresholds=th, risk_cfg=RiskConfig(), initial_cash=10_000)
    result = bt.run(ohlcv=synthetic_ohlcv)
    assert result.metrics.num_trades == 0
    # Equity should be flat (cash only)
    assert result.equity_curve.iloc[0] == result.equity_curve.iloc[-1] == 10_000


def test_backtest_rejects_unsorted_real_ohlcv(synthetic_ohlcv: pd.DataFrame) -> None:
    invalid = synthetic_ohlcv.iloc[[1, 0, *range(2, len(synthetic_ohlcv))]]
    bt = Backtester(thresholds=ThresholdConfig(), risk_cfg=RiskConfig(), initial_cash=10_000)

    with pytest.raises(ValueError, match="sorted"):
        bt.run(ohlcv=invalid)


def test_backtest_rejects_invalid_ohlcv_bar(synthetic_ohlcv: pd.DataFrame) -> None:
    invalid = synthetic_ohlcv.copy()
    invalid.iloc[0, invalid.columns.get_loc("high")] = invalid["close"].iloc[0] * 0.5
    bt = Backtester(thresholds=ThresholdConfig(), risk_cfg=RiskConfig(), initial_cash=10_000)

    with pytest.raises(ValueError, match="high"):
        bt.run(ohlcv=invalid)


def test_ohlcv_fingerprint_is_stable_and_changes_with_data(
    synthetic_ohlcv: pd.DataFrame,
) -> None:
    original = ohlcv_fingerprint(synthetic_ohlcv)
    same = ohlcv_fingerprint(synthetic_ohlcv.copy())
    changed = synthetic_ohlcv.copy()
    changed.iloc[-1, changed.columns.get_loc("close")] += 1.0

    assert original == same
    assert original != ohlcv_fingerprint(changed)
