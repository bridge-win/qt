from __future__ import annotations

import pandas as pd
import pytest
from scipy.stats import kurtosis, norm, skew

from qt.research.analysis import (
    block_bootstrap_summary,
    deflated_sharpe_details,
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


def test_deflated_sharpe_requires_cross_trial_sharpe_variance_for_multiple_testing() -> None:
    returns = pd.Series([0.01, -0.003, 0.008, -0.002] * 20, dtype="float64")

    details = deflated_sharpe_details(
        returns,
        attempted_variants=10,
        trial_sharpe_variance=None,
        trial_scope={"candidate_evaluations": 10},
    )

    assert details["status"] == "not_applicable"
    assert "raw-return volatility" in str(details["reason"])
    assert deflated_sharpe_probability(returns, attempted_variants=10) == 0.0


def test_deflated_sharpe_is_return_scale_invariant_and_matches_reference_formula() -> None:
    returns = pd.Series([0.01, -0.004, 0.007, -0.002, 0.006] * 40, dtype="float64")
    variance = 0.04
    details = deflated_sharpe_details(
        returns,
        attempted_variants=5,
        trial_sharpe_variance=variance,
    )
    scaled = deflated_sharpe_details(
        returns * 100,
        attempted_variants=5,
        trial_sharpe_variance=variance,
    )

    assert details["status"] == scaled["status"] == "computed"
    assert details["value"] == pytest.approx(scaled["value"])
    observed = float(returns.mean() / returns.std(ddof=1))
    gamma = 0.5772156649015329
    expected_max = variance**0.5 * (
        (1 - gamma) * norm.ppf(1 - 1 / 5) + gamma * norm.ppf(1 - 1 / (5 * 2.718281828459045))
    )
    denominator = (
        1
        - float(skew(returns, bias=False)) * observed
        + ((float(kurtosis(returns, fisher=False, bias=False)) - 1) / 4) * observed**2
    ) ** 0.5
    reference = norm.cdf((observed - expected_max) * (len(returns) - 1) ** 0.5 / denominator)
    assert details["value"] == pytest.approx(reference)


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
