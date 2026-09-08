# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CandidateAcceptancePolicy:
    min_splits: int = 6
    min_pass_ratio: float = 0.8
    min_average_profit_total_pct: float = 0.0
    min_total_profit_total_pct: float = 0.0
    max_worst_drawdown_pct: float = 15.0
    max_parameter_cv: float = 0.35
    min_bootstrap_mean_p05_pct: float = 0.0
    max_bootstrap_loss_probability: float = 0.25
    bootstrap_iterations: int = 5000
    bootstrap_seed: int = 42
    max_exact_bootstrap_samples: int = 100_000

    def __post_init__(self) -> None:
        if self.min_splits <= 0:
            raise ValueError("min_splits must be positive")
        if not 0 <= self.min_pass_ratio <= 1:
            raise ValueError("min_pass_ratio must be between 0 and 1")
        if self.max_worst_drawdown_pct < 0:
            raise ValueError("max_worst_drawdown_pct cannot be negative")
        if self.max_parameter_cv < 0:
            raise ValueError("max_parameter_cv cannot be negative")
        if not 0 <= self.max_bootstrap_loss_probability <= 1:
            raise ValueError("max_bootstrap_loss_probability must be between 0 and 1")
        if self.bootstrap_iterations <= 0:
            raise ValueError("bootstrap_iterations must be positive")
        if self.max_exact_bootstrap_samples <= 0:
            raise ValueError("max_exact_bootstrap_samples must be positive")


def write_candidate_acceptance_report(
    *,
    result_path: Path,
    policy: CandidateAcceptancePolicy = CandidateAcceptancePolicy(),
    root_dir: Path | None = None,
) -> Path:
    root = root_dir or result_path.parent
    payload = json.loads(result_path.read_text())
    report = evaluate_candidate_acceptance(payload, policy=policy, root_dir=root)
    report_path = result_path.with_name("candidate_acceptance.json")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report_path


def evaluate_candidate_acceptance(
    payload: dict[str, Any],
    *,
    policy: CandidateAcceptancePolicy = CandidateAcceptancePolicy(),
    root_dir: Path,
) -> dict[str, Any]:
    evaluation = payload.get("evaluation")
    aggregate = evaluation.get("aggregate") if isinstance(evaluation, dict) else None
    splits = payload.get("splits")
    split_count = len(splits) if isinstance(splits, list) else 0
    parameter_stability = _parameter_stability(
        splits if isinstance(splits, list) else [],
        root_dir=root_dir,
    )
    return_confidence = _return_confidence(splits if isinstance(splits, list) else [], policy=policy)

    checks = {
        "average_profit_total_pct": _min_numeric_check(
            aggregate,
            key="average_profit_total_pct",
            minimum=policy.min_average_profit_total_pct,
        ),
        "min_splits": _minimum_splits_check(split_count=split_count, minimum=policy.min_splits),
        "parameter_stability": _parameter_stability_check(parameter_stability, max_cv=policy.max_parameter_cv),
        "pass_ratio": _min_numeric_check(aggregate, key="pass_ratio", minimum=policy.min_pass_ratio),
        "return_confidence": _return_confidence_check(return_confidence, policy=policy),
        "total_profit_total_pct": _min_numeric_check(
            aggregate,
            key="total_profit_total_pct",
            minimum=policy.min_total_profit_total_pct,
        ),
        "worst_max_drawdown_pct": _max_numeric_check(
            aggregate,
            key="worst_max_drawdown_pct",
            maximum=policy.max_worst_drawdown_pct,
        ),
    }
    failed_reasons = [check["reason"] for check in checks.values() if check["reason"] is not None]
    return {
        "checks": checks,
        "failed_reasons": failed_reasons,
        "parameter_stability": parameter_stability,
        "passed": not failed_reasons,
        "policy": asdict(policy),
        "return_confidence": return_confidence,
    }


def _minimum_splits_check(*, split_count: int, minimum: int) -> dict[str, bool | str | None]:
    if split_count < minimum:
        return _failed(f"split_count below {minimum}")
    return _passed()


def _min_numeric_check(
    aggregate: object,
    *,
    key: str,
    minimum: float,
) -> dict[str, bool | str | None]:
    if not isinstance(aggregate, dict) or not isinstance(aggregate.get(key), (int, float)):
        return _failed(f"missing {key}")
    if not _is_finite_number(aggregate[key]):
        return _failed(f"{key} is non-finite")
    if float(aggregate[key]) < minimum:
        return _failed(f"{key} below {minimum}")
    return _passed()


def _max_numeric_check(
    aggregate: object,
    *,
    key: str,
    maximum: float,
) -> dict[str, bool | str | None]:
    if not isinstance(aggregate, dict) or not isinstance(aggregate.get(key), (int, float)):
        return _failed(f"missing {key}")
    if not _is_finite_number(aggregate[key]):
        return _failed(f"{key} is non-finite")
    if float(aggregate[key]) > maximum:
        return _failed(f"{key} above {maximum}")
    return _passed()


def _parameter_stability_check(
    stability: dict[str, dict[str, Any]],
    *,
    max_cv: float,
) -> dict[str, bool | str | None]:
    if not stability:
        return _failed("missing optimized parameter evidence")
    invalid = [
        name
        for name, stats in sorted(stability.items())
        if stats.get("invalid_reason") is not None
    ]
    if invalid:
        return _failed(f"non-finite optimized parameters: {', '.join(invalid)}")
    unstable = [
        name
        for name, stats in sorted(stability.items())
        if isinstance(stats.get("coefficient_of_variation"), (int, float))
        and float(stats["coefficient_of_variation"]) > max_cv
    ]
    if unstable:
        return _failed(f"unstable parameters: {', '.join(unstable)}")
    return _passed()


def _return_confidence_check(
    confidence: dict[str, Any],
    *,
    policy: CandidateAcceptancePolicy,
) -> dict[str, bool | str | None]:
    if confidence.get("invalid_reason") is not None:
        return _failed(str(confidence["invalid_reason"]))
    if not confidence.get("split_profit_total_pct"):
        return _failed("missing return confidence evidence")

    failed_reasons = []
    p05 = confidence.get("bootstrap_mean_p05_pct")
    if not _is_finite_number(p05):
        failed_reasons.append("bootstrap_mean_p05_pct is non-finite")
    elif p05 < policy.min_bootstrap_mean_p05_pct:
        failed_reasons.append(f"bootstrap_mean_p05_pct below {policy.min_bootstrap_mean_p05_pct}")

    loss_probability = confidence.get("bootstrap_loss_probability")
    if (
        not _is_finite_number(loss_probability)
        or loss_probability > policy.max_bootstrap_loss_probability
    ):
        failed_reasons.append(
            "bootstrap_loss_probability is non-finite"
            if not _is_finite_number(loss_probability)
            else f"bootstrap_loss_probability above {policy.max_bootstrap_loss_probability}"
        )

    if failed_reasons:
        return _failed("; ".join(failed_reasons))
    return _passed()


def _return_confidence(
    splits: list[object],
    *,
    policy: CandidateAcceptancePolicy,
) -> dict[str, Any]:
    returns, invalid_returns = _split_profit_values(splits)
    if invalid_returns:
        return {
            "bootstrap_loss_probability": None,
            "bootstrap_mean_p05_pct": None,
            "bootstrap_mean_p50_pct": None,
            "bootstrap_mean_p95_pct": None,
            "invalid_reason": "non-finite split profit_total_pct",
            "method": "none",
            "sample_count": 0,
            "split_profit_total_pct": [],
        }
    if not returns:
        return {
            "bootstrap_loss_probability": None,
            "bootstrap_mean_p05_pct": None,
            "bootstrap_mean_p50_pct": None,
            "bootstrap_mean_p95_pct": None,
            "method": "none",
            "sample_count": 0,
            "split_profit_total_pct": [],
        }

    sample_count = len(returns) ** len(returns)
    if sample_count <= policy.max_exact_bootstrap_samples:
        sample_means = _exact_bootstrap_means(returns)
        method = "exhaustive"
    else:
        sample_means = _sampled_bootstrap_means(
            returns,
            iterations=policy.bootstrap_iterations,
            seed=policy.bootstrap_seed,
        )
        method = "sampled"

    return {
        "bootstrap_loss_probability": _round(sum(1 for value in sample_means if value < 0) / len(sample_means)),
        "bootstrap_mean_p05_pct": _round(_nearest_rank_percentile(sample_means, 0.05)),
        "bootstrap_mean_p50_pct": _round(_nearest_rank_percentile(sample_means, 0.50)),
        "bootstrap_mean_p95_pct": _round(_nearest_rank_percentile(sample_means, 0.95)),
        "method": method,
        "sample_count": len(sample_means),
        "split_profit_total_pct": [_round(value) for value in returns],
    }


def _split_profit_values(splits: list[object]) -> tuple[list[float], bool]:
    values = []
    invalid = False
    for split in splits:
        if not isinstance(split, dict):
            continue
        summary = split.get("summary")
        if not isinstance(summary, dict):
            continue
        profit_total_pct = summary.get("profit_total_pct")
        if isinstance(profit_total_pct, (int, float)):
            numeric_value = float(profit_total_pct)
            if not math.isfinite(numeric_value):
                invalid = True
            else:
                values.append(numeric_value)
    return values, invalid


def _exact_bootstrap_means(values: list[float]) -> list[float]:
    sample_size = len(values)
    means = [
        sum(sample) / sample_size
        for sample in product(values, repeat=sample_size)
    ]
    return sorted(means)


def _sampled_bootstrap_means(
    values: list[float],
    *,
    iterations: int,
    seed: int,
) -> list[float]:
    rng = random.Random(seed)
    sample_size = len(values)
    means = [
        sum(rng.choice(values) for _ in range(sample_size)) / sample_size
        for _ in range(iterations)
    ]
    return sorted(means)


def _nearest_rank_percentile(sorted_values: list[float], percentile: float) -> float:
    index = max(0, math.ceil(percentile * len(sorted_values)) - 1)
    return sorted_values[index]


def _parameter_stability(splits: list[object], *, root_dir: Path) -> dict[str, dict[str, Any]]:
    values_by_name: dict[str, list[float]] = {}
    for split in splits:
        if not isinstance(split, dict):
            continue
        params_path = _optimized_params_path(split, root_dir=root_dir)
        if params_path is None or not params_path.exists():
            continue
        payload = json.loads(params_path.read_text())
        params = payload.get("params")
        if isinstance(params, dict):
            for name, value in _flatten_numeric_params(params).items():
                values_by_name.setdefault(name, []).append(value)

    return {
        name: _numeric_stability(values)
        for name, values in sorted(values_by_name.items())
    }


def _optimized_params_path(split: dict[str, object], *, root_dir: Path) -> Path | None:
    optimization_result = split.get("optimization_result")
    if not isinstance(optimization_result, dict):
        return None
    optimized_params = optimization_result.get("optimized_params")
    if not isinstance(optimized_params, dict) or not isinstance(optimized_params.get("path"), str):
        return None
    path = Path(optimized_params["path"])
    return path if path.is_absolute() else root_dir / path


def _flatten_numeric_params(params: dict[str, object], *, prefix: str = "") -> dict[str, float]:
    flattened: dict[str, float] = {}
    for key, value in params.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flattened.update(_flatten_numeric_params(value, prefix=name))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            flattened[name] = float(value)
    return flattened


def _numeric_stability(values: list[float]) -> dict[str, Any]:
    if not values or any(not math.isfinite(value) for value in values):
        return {"invalid_reason": "non-finite optimized parameter", "values": []}
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    standard_deviation = math.sqrt(variance)
    coefficient_of_variation = standard_deviation / abs(mean) if mean else 0.0
    return {
        "coefficient_of_variation": _round(coefficient_of_variation),
        "max": _round(max(values)),
        "mean": _round(mean),
        "min": _round(min(values)),
        "values": [_round(value) for value in values],
    }


def _round(value: float) -> float:
    return round(value, 10)


def _is_finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _passed() -> dict[str, bool | str | None]:
    return {"passed": True, "reason": None}


def _failed(reason: str) -> dict[str, bool | str | None]:
    return {"passed": False, "reason": reason}

