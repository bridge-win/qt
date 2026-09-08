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
    trial_sharpe_variance: float | None = None,
) -> float:
    """Return DSR only when the variance of trial Sharpe estimates is known.

    A return-series volatility is not the dispersion of estimated Sharpe
    ratios across the search.  When the latter is absent, return zero so
    existing scalar consumers remain conservative; callers that can present
    research evidence should use :func:`deflated_sharpe_details` instead.
    """

    details = deflated_sharpe_details(
        returns,
        attempted_variants=attempted_variants,
        trial_sharpe_variance=trial_sharpe_variance,
    )
    value = details.get("value")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def deflated_sharpe_details(
    returns: pd.Series | None,
    *,
    attempted_variants: int,
    trial_sharpe_variance: float | None,
    trial_scope: JsonDict | None = None,
) -> JsonDict:
    """Compute Bailey--López de Prado DSR with an explicit trial-SR variance.

    The selection threshold is ``sqrt(V[SR_hat]) * ((1-gamma) z_1 + gamma z_2)``.
    It is scale-invariant because both the observed Sharpe and ``V[SR_hat]``
    are dimensionless.  The required variance must come from comparable trial
    Sharpe estimates, never raw-return volatility.
    """

    scope = trial_scope or {}
    if attempted_variants < 1:
        return {
            "status": "not_applicable",
            "reason": "attempted_variants must be positive",
            "multiple_testing_scope": scope,
        }
    if returns is None:
        return {
            "status": "not_applicable",
            "reason": "native executor did not provide OOS return series",
            "multiple_testing_scope": scope,
        }
    values = returns.dropna().astype("float64")
    if len(values) < 3 or not np.isfinite(values.to_numpy()).all():
        return {
            "status": "not_applicable",
            "reason": "at least three finite OOS returns are required",
            "multiple_testing_scope": scope,
        }
    standard_deviation = float(values.std(ddof=1))
    if standard_deviation <= 0 or not math.isfinite(standard_deviation):
        return {
            "status": "not_applicable",
            "reason": "OOS returns have no finite sample volatility",
            "multiple_testing_scope": scope,
        }
    if attempted_variants > 1 and trial_sharpe_variance is None:
        return {
            "status": "not_applicable",
            "reason": (
                "multiple-trial DSR requires variance of comparable trial Sharpe estimates; "
                "raw-return volatility is not a valid substitute"
            ),
            "multiple_testing_scope": scope,
        }
    variance = 0.0 if trial_sharpe_variance is None else float(trial_sharpe_variance)
    if variance < 0 or not math.isfinite(variance):
        return {
            "status": "not_applicable",
            "reason": "trial_sharpe_variance must be finite and non-negative",
            "multiple_testing_scope": scope,
        }
    observed = float(values.mean() / standard_deviation)
    trials = max(attempted_variants, 1)
    expected_max = 0.0
    if trials > 1:
        euler_gamma = 0.5772156649015329
        first = norm.ppf(1 - 1 / trials)
        second = norm.ppf(1 - 1 / (trials * math.e))
        expected_max = float(
            math.sqrt(variance)
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
    return {
        "status": "computed",
        "value": float(max(0.0, min(1.0, norm.cdf(statistic)))),
        "observed_sharpe": observed,
        "expected_max_sharpe": expected_max,
        "trial_sharpe_variance": variance,
        "multiple_testing_scope": scope,
        "reference": "Bailey and Lopez de Prado (2014), Deflated Sharpe Ratio",
    }


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
