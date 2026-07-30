from __future__ import annotations

import pandas as pd

from qt.research.analysis import (
    block_bootstrap_summary,
    deflated_sharpe_probability,
)


def test_deflated_sharpe_penalizes_more_attempted_variants() -> None:
    returns = pd.Series(
        [0.01, -0.005, 0.008, 0.002, -0.003, 0.006] * 30,
        dtype="float64",
    )

    one_trial = deflated_sharpe_probability(returns, attempted_variants=1)
    many_trials = deflated_sharpe_probability(returns, attempted_variants=25)

    assert 0 <= many_trials <= one_trial <= 1


def test_block_bootstrap_is_seeded_and_reports_loss_probability() -> None:
    returns = pd.Series([0.01, -0.02, 0.015, 0.005, -0.004] * 20)

    first = block_bootstrap_summary(
        returns,
        simulations=100,
        block_size=5,
        seed=7,
    )
    second = block_bootstrap_summary(
        returns,
        simulations=100,
        block_size=5,
        seed=7,
    )

    assert first == second
    assert 0 <= first["loss_probability"] <= 1
    assert first["simulations"] == 100
    assert len(first["percentile_paths"]["p05"]) == len(returns) + 1
