"""Selection-aware statistics with explicit data requirements and provenance.

Sharpe inputs for DSR are per observation, never annualized. PBO implements
CSCV over a complete common time-block x candidate net-return matrix. Neither
method establishes causality or supplies independent out-of-time validation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from math import ceil, isfinite, log, sqrt
from typing import Any

import numpy as np
from scipy.stats import kurtosis, norm, rankdata, skew


def _unavailable(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "not_applicable", "value": None, "reason": reason, **details}


def _finite_vector(values: Sequence[float]) -> np.ndarray:
    result = np.asarray(values, dtype=float)
    if result.ndim != 1 or not np.all(np.isfinite(result)):
        raise ValueError("expected_finite_vector")
    return result


@dataclass(frozen=True)
class CandidateSharpe:
    trial_id: str
    sharpe_per_observation: float
    comparison_key: str


def deflated_sharpe(
    returns: Sequence[float],
    candidates: Sequence[CandidateSharpe],
    *,
    expected_trial_ids: Sequence[str],
    comparison_key: str,
    effective_observations: int | None = None,
) -> dict[str, Any]:
    """Bailey/Lopez de Prado DSR using actual comparable trial Sharpe variance.

    The comparison key must bind dataset, split, costs, clock and Sharpe scale.
    Expected IDs come from the immutable campaign ledger, not the winner's
    payload. Failed/missing trial outcomes make the correction unavailable.
    """
    values = _finite_vector(returns)
    ids = [candidate.trial_id for candidate in candidates]
    expected = list(expected_trial_ids)
    if len(set(ids)) != len(ids) or len(set(expected)) != len(expected):
        raise ValueError("duplicate_trial_id")
    if not expected or set(ids) != set(expected):
        return _unavailable("incomplete_campaign_sharpe_population")
    if len(ids) < 2:
        return _unavailable("requires_multiple_comparable_trials")
    if not comparison_key or any(c.comparison_key != comparison_key for c in candidates):
        return _unavailable("incomparable_trial_sharpes")
    if any(not isfinite(c.sharpe_per_observation) for c in candidates):
        raise ValueError("non_finite_trial_sharpe")
    count = len(values) if effective_observations is None else effective_observations
    if not isinstance(count, int) or count > len(values) or count < 30:
        return _unavailable("insufficient_effective_observations", observations=len(values))
    std = float(np.std(values, ddof=1))
    if std == 0:
        return _unavailable("zero_return_variance")
    trial_variance = float(np.var([c.sharpe_per_observation for c in candidates], ddof=1))
    if trial_variance <= 0:
        return _unavailable("zero_comparable_trial_sharpe_variance")
    n_trials = len(candidates)
    euler_gamma = 0.5772156649015329
    benchmark = sqrt(trial_variance) * (
        (1 - euler_gamma) * norm.ppf(1 - 1 / n_trials)
        + euler_gamma * norm.ppf(1 - 1 / (n_trials * np.e))
    )
    observed = float(np.mean(values)) / std
    asymmetry = float(skew(values, bias=False))
    pearson_kurtosis = float(kurtosis(values, fisher=False, bias=False))
    denominator_squared = 1 - asymmetry * observed + (pearson_kurtosis - 1) / 4 * observed**2
    if not isfinite(denominator_squared) or denominator_squared <= 0:
        return _unavailable("invalid_sharpe_sampling_variance")
    z_score = (observed - benchmark) * sqrt(count - 1) / sqrt(denominator_squared)
    return {
        "status": "computed",
        "value": float(norm.cdf(z_score)),
        "observed_sharpe_per_observation": observed,
        "benchmark_sharpe_per_observation": float(benchmark),
        "trial_sharpe_variance": trial_variance,
        "n_trials": n_trials,
        "effective_observations": count,
        "independent_trials_assumed": True,
        "comparison_key": comparison_key,
        "scope": "complete_declared_campaign_only",
    }


def probability_of_backtest_overfitting(
    performance_matrix: Sequence[Sequence[float]] | None,
    *,
    candidate_ids: Sequence[str],
    expected_trial_ids: Sequence[str],
) -> dict[str, Any]:
    """CSCV PBO with average ties and explicit complete population checks."""
    if performance_matrix is None:
        return _unavailable("missing_common_candidate_performance_matrix")
    ids, expected = list(candidate_ids), list(expected_trial_ids)
    if len(set(ids)) != len(ids) or len(set(expected)) != len(expected):
        raise ValueError("duplicate_trial_id")
    if set(ids) != set(expected) or not expected:
        return _unavailable("incomplete_campaign_performance_matrix")
    matrix = np.asarray(performance_matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] != len(ids) or not np.all(np.isfinite(matrix)):
        return _unavailable("invalid_or_incomplete_performance_matrix")
    blocks, candidate_count = matrix.shape
    if candidate_count < 2 or blocks < 4 or blocks % 2:
        return _unavailable("requires_even_time_blocks_and_multiple_candidates")
    if blocks > 16:
        raise ValueError("cscv_partition_budget_exceeded")
    if all(np.allclose(matrix[:, 0], matrix[:, i], rtol=0, atol=0) for i in range(1, candidate_count)):
        return _unavailable("identical_candidate_performance")

    def sharpe_score(samples: np.ndarray) -> np.ndarray:
        means = samples.mean(axis=0)
        scales = samples.std(axis=0, ddof=1)
        result = np.zeros(candidate_count)
        np.divide(means, scales, out=result, where=scales != 0)
        result[(scales == 0) & (means > 0)] = np.inf
        result[(scales == 0) & (means < 0)] = -np.inf
        return result

    logits = []
    for selected in combinations(range(blocks), blocks // 2):
        test = [i for i in range(blocks) if i not in selected]
        training_scores = sharpe_score(matrix[list(selected), :])
        winners = np.flatnonzero(training_scores == np.max(training_scores))
        out_scores = sharpe_score(matrix[test, :])
        ranks = rankdata(out_scores, method="average")
        # Average tied selected winners instead of arbitrary column-order tie breaking.
        omega = float(np.mean(ranks[winners])) / (candidate_count + 1)
        logits.append(log(omega / (1 - omega)))
    return {
        "status": "computed",
        "value": sum(value <= 0 for value in logits) / len(logits),
        "partitions": len(logits),
        "time_blocks": blocks,
        "candidate_ids": ids,
        "logits": logits,
        "scope": "complete_declared_campaign_only",
        "independent_paths": False,
    }


def holm_adjust(p_values: Sequence[float]) -> tuple[float, ...]:
    values = _finite_vector(p_values)
    if np.any((values < 0) | (values > 1)):
        raise ValueError("invalid_p_value")
    order = np.argsort(values, kind="stable")
    adjusted = np.zeros(len(values))
    running = 0.0
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (len(values) - rank) * float(values[index])))
        adjusted[index] = running
    return tuple(float(value) for value in adjusted)


def block_bootstrap_mean(
    values: Sequence[float],
    *,
    block_size: int,
    resamples: int = 10_000,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, Any]:
    """Circular moving-block bootstrap, preserving dependence within each block.

    Choose block size before inspecting the result. A daily series is not
    automatically an independent sample; this reports a percentile interval,
    not a multiple-testing-corrected p-value.
    """
    data = _finite_vector(values)
    if not 0 < confidence < 1 or resamples < 100 or block_size < 1:
        raise ValueError("invalid_bootstrap_configuration")
    if len(data) < 2 * block_size:
        return _unavailable("insufficient_blocks", observations=len(data))
    rng = np.random.default_rng(seed)
    means = np.empty(resamples)
    for offset in range(0, resamples, 500):
        batch = min(500, resamples - offset)
        starts = rng.integers(0, len(data), size=(batch, ceil(len(data) / block_size)))
        indices = (starts[:, :, None] + np.arange(block_size)) % len(data)
        samples = data[indices.reshape(batch, -1)[:, : len(data)]]
        means[offset : offset + batch] = samples.mean(axis=1)
    alpha = (1 - confidence) / 2
    lower, upper = np.quantile(means, [alpha, 1 - alpha])
    return {
        "status": "computed",
        "value": float(data.mean()),
        "lower": float(lower),
        "upper": float(upper),
        "confidence": confidence,
        "observations": len(data),
        "blocks": len(data) // block_size,
        "block_size": block_size,
        "resamples": resamples,
        "seed": seed,
        "method": "circular_moving_block_percentile",
    }
