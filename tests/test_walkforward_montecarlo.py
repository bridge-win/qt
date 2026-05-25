"""Walk-forward + bootstrap tests on the synthetic OHLCV fixture."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qt.backtest.montecarlo import (
    bootstrap_trade_returns,
    deflated_sharpe,
    stationary_bootstrap,
)
from qt.backtest.walkforward import generate_windows, run_walk_forward
from qt.core.config import RiskConfig, ThresholdConfig


def test_generate_windows() -> None:
    idx = pd.date_range("2020-01-01", "2024-01-01", freq="1h", tz="UTC")
    wins = generate_windows(idx, train_days=365, test_days=90, step_days=180)
    assert len(wins) >= 3
    for ts, te, vs, ve in wins:
        assert vs == te
        assert (te - ts).days == 365
        assert (ve - vs).days == 90


def test_walkforward_runs_on_synthetic(synthetic_ohlcv: pd.DataFrame) -> None:
    # Synthetic is only 6 months — use very small windows.
    base = ThresholdConfig(
        rsi_oversold=30, bb_std=1.5, drawdown_30d_min=0.05,
        wick_body_ratio_min=2.0, rv_ratio_min=1.3,
        entry_score_min=0.35, min_factor_groups=1,
        vix_max=999, dxy_z_max=999,
    )
    grid = [{"entry_score_min": x, "min_factor_groups": 1} for x in (0.3, 0.5)]
    r = run_walk_forward(
        ohlcv=synthetic_ohlcv,
        aux_inputs={},
        base_thresholds=base,
        risk_cfg=RiskConfig(),
        train_days=60, test_days=30, step_days=30,
        grid=grid,
        min_trades_to_select=0,
    )
    assert len(r.windows) >= 1


def test_bootstrap_trade_returns() -> None:
    rng = np.random.default_rng(0)
    rets = rng.normal(0.02, 0.05, size=30)
    stats = bootstrap_trade_returns(rets, n_iter=300, seed=0)
    assert stats.n_iter == 300
    assert stats.p05_total_return <= stats.mean_total_return <= stats.p95_total_return


def test_stationary_bootstrap_shape() -> None:
    rets = np.random.default_rng(0).normal(0, 0.01, size=500)
    out = stationary_bootstrap(rets, n_iter=50, block_mean=10.0, seed=0)
    assert out.shape == (50,)


def test_deflated_sharpe_in_unit_interval() -> None:
    dsr = deflated_sharpe(1.0, n_trials=50, sample_size=500)
    assert 0.0 <= dsr <= 1.0
