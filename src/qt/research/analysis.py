"""Statistical robustness summaries used by standard research jobs."""

from __future__ import annotations

import math
from typing import TypeAlias

import numpy as np
import pandas as pd
from btc_backtest.validation.monte_carlo import BlockBootstrap
from scipy.stats import kurtosis, norm, skew

JsonDict: TypeAlias = dict[str, object]


def deflated_sharpe_probability(
    returns: pd.Series,
    *,
    attempted_variants: int,
) -> float:
    values = returns.dropna().astype("float64")
    if len(values) < 3 or attempted_variants < 1:
        return 0.0
    standard_deviation = float(values.std(ddof=1))
    if standard_deviation <= 0 or not math.isfinite(standard_deviation):
        return 0.0
    observed = float(values.mean() / standard_deviation)
    trials = max(attempted_variants, 1)
    expected_max = 0.0
    if trials > 1:
        euler_gamma = 0.5772156649015329
        first = norm.ppf(1 - 1 / trials)
        second = norm.ppf(1 - 1 / (trials * math.e))
        expected_max = float(
            standard_deviation
            * ((1 - euler_gamma) * first + euler_gamma * second)
        )
    skewness = float(skew(values, bias=False))
    excess_kurtosis = float(kurtosis(values, fisher=False, bias=False))
    denominator = math.sqrt(
        max(
            1e-12,
            1
            - skewness * observed
            + ((excess_kurtosis - 1) / 4) * observed**2,
        )
    )
    statistic = (
        (observed - expected_max) * math.sqrt(len(values) - 1) / denominator
    )
    return float(max(0.0, min(1.0, norm.cdf(statistic))))


def block_bootstrap_summary(
    returns: pd.Series,
    *,
    simulations: int,
    block_size: int,
    seed: int,
) -> JsonDict:
    result = BlockBootstrap.run(
        returns,
        simulations=simulations,
        block_size=block_size,
        seed=seed,
    )
    cumulative = np.asarray(
        [
            np.concatenate(
                (
                    np.asarray([1.0]),
                    np.cumprod(1 + np.asarray(path, dtype="float64")),
                )
            )
            for path in result.paths
        ],
        dtype="float64",
    )
    final = cumulative[:, -1] - 1
    return {
        "simulations": simulations,
        "block_size": block_size,
        "seed": seed,
        "loss_probability": float(np.mean(final < 0)),
        "return_p05": float(np.quantile(final, 0.05)),
        "return_p50": float(np.quantile(final, 0.50)),
        "return_p95": float(np.quantile(final, 0.95)),
        "percentile_paths": {
            "p05": np.quantile(cumulative, 0.05, axis=0).tolist(),
            "p50": np.quantile(cumulative, 0.50, axis=0).tolist(),
            "p95": np.quantile(cumulative, 0.95, axis=0).tolist(),
        },
    }
