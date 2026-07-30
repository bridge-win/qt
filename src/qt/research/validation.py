"""Evidence-based research verdicts with no live-trading approval state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeAlias

JsonDict: TypeAlias = dict[str, object]


def evaluate_research_verdict(evidence: Mapping[str, object]) -> JsonDict:
    failed_integrity: list[str] = []
    if evidence.get("data_complete") is not True:
        failed_integrity.append("Data coverage is incomplete.")
    if evidence.get("lookahead_consistent") is not True:
        failed_integrity.append("Lookahead consistency gate failed.")
    if evidence.get("recursive_stable") is not True:
        failed_integrity.append("Warm-up and recursive stability gate failed.")
    if failed_integrity:
        return _verdict("rejected", failed_integrity)

    out_of_sample = _mapping(evidence.get("out_of_sample"))
    cost_stress = _mapping(evidence.get("cost_stress"))
    cost_2x = _mapping(cost_stress.get("2x"))
    monte_carlo = _mapping(evidence.get("monte_carlo"))
    fragile: list[str] = []
    if _number(out_of_sample.get("sharpe")) < 0.5:
        fragile.append("Final out-of-sample Sharpe is below 0.5.")
    if _number(out_of_sample.get("max_drawdown")) < -0.35:
        fragile.append("Final out-of-sample drawdown exceeds 35%.")
    if _number(cost_2x.get("total_return")) <= 0:
        fragile.append("Return is not positive under 2x trading costs.")
    if _number(evidence.get("deflated_sharpe_probability")) < 0.90:
        fragile.append("Deflated Sharpe probability is below 90%.")
    if _number(monte_carlo.get("loss_probability")) > 0.20:
        fragile.append("Monte Carlo loss probability exceeds 20%.")
    if fragile:
        return _verdict("fragile", fragile)

    benchmarks = _mapping(evidence.get("benchmarks"))
    buy_hold = _mapping(benchmarks.get("buy_and_hold"))
    oos_return = _number(out_of_sample.get("total_return"))
    benchmark_return = _number(buy_hold.get("total_return"))
    oos_drawdown = abs(_number(out_of_sample.get("max_drawdown")))
    benchmark_drawdown = abs(_number(buy_hold.get("max_drawdown")))
    beats_return = oos_return > benchmark_return
    reduces_drawdown = (
        oos_return > 0
        and benchmark_drawdown > 0
        and oos_drawdown <= benchmark_drawdown * 0.80
    )
    if not beats_return and not reduces_drawdown:
        return _verdict(
            "fragile",
            ["The final test neither beats buy-and-hold nor reduces its drawdown by 20%."],
        )
    return _verdict("paper-research-candidate", [])


def _verdict(status: str, failed_gates: list[str]) -> JsonDict:
    labels = {
        "rejected": "Rejected: integrity evidence failed.",
        "fragile": "Fragile: useful for learning, not ready for paper evaluation.",
        "paper-research-candidate": (
            "Paper-research candidate: validate with forward paper fills before any live use."
        ),
    }
    return {
        "status": status,
        "label": labels[status],
        "failed_gates": failed_gates,
        "live_ready": False,
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)
