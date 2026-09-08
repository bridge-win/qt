# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Final


@dataclass(frozen=True)
class PromotionPolicy:
    min_oos_return: float = 0.0
    min_profit_factor: float = 1.2
    max_drawdown_pct: float = 15.0
    min_trades: int = 50
    min_sortino: float = 0.5


_SUMMARY_METRICS: Final[tuple[tuple[str, str, str], ...]] = (
    ("profit_total_pct", "below", "min_oos_return"),
    ("profit_factor", "below", "min_profit_factor"),
    ("max_drawdown_pct", "above", "max_drawdown_pct"),
    ("total_trades", "below", "min_trades"),
    ("sortino", "below", "min_sortino"),
)
_REQUIRED_PASSED_EVIDENCE: Final[tuple[str, ...]] = (
    "walk_forward",
    "robustness",
    "regime",
    "dry_run",
    "operational",
)


def evaluate_candidate_promotion(
    candidate_id: str,
    reports: dict[str, dict[str, object]],
    policy: PromotionPolicy,
) -> dict[str, object]:
    """Evaluate a candidate using final OOS metrics and evidence only."""
    failed_reasons: list[str] = []
    final_oos = reports.get("final_oos")
    summary = final_oos.get("summary") if isinstance(final_oos, dict) else None

    if not isinstance(final_oos, dict):
        failed_reasons.append("missing final_oos report")
    if not isinstance(summary, dict):
        failed_reasons.append("final_oos missing summary")
    else:
        for metric, comparison, policy_field in _SUMMARY_METRICS:
            value = summary.get(metric)
            minimum_or_maximum = getattr(policy, policy_field)
            if not _is_finite_number(value):
                failed_reasons.append(f"final_oos missing {metric}")
            elif comparison == "below" and value < minimum_or_maximum:
                failed_reasons.append(f"final_oos {metric} below {minimum_or_maximum}")
            elif comparison == "above" and value > minimum_or_maximum:
                failed_reasons.append(f"final_oos {metric} above {minimum_or_maximum}")

    if isinstance(final_oos, dict):
        _check_bias(final_oos, failed_reasons)
        for evidence_name in _REQUIRED_PASSED_EVIDENCE:
            _check_passed_evidence(final_oos, evidence_name, failed_reasons)

    return {
        "candidate_id": candidate_id,
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
        "policy": asdict(policy),
        "reports": reports,
    }


def _check_bias(report: dict[str, object], failed_reasons: list[str]) -> None:
    evidence = report.get("bias")
    if not isinstance(evidence, dict):
        failed_reasons.append("final_oos missing bias evidence")
        return
    if evidence.get("lookahead_passed") is not True:
        failed_reasons.append("final_oos bias lookahead check failed")
    if evidence.get("recursive_passed") is not True:
        failed_reasons.append("final_oos bias recursive check failed")


def _check_passed_evidence(
    report: dict[str, object], evidence_name: str, failed_reasons: list[str]
) -> None:
    evidence = report.get(evidence_name)
    if not isinstance(evidence, dict):
        failed_reasons.append(f"final_oos missing {evidence_name} evidence")
    elif evidence.get("passed") is not True:
        failed_reasons.append(f"final_oos {evidence_name} check failed")


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

