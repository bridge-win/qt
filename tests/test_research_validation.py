from __future__ import annotations

from qt.research.validation import evaluate_research_verdict


def _evidence() -> dict[str, object]:
    return {
        "data_complete": True,
        "lookahead_consistent": True,
        "recursive_stable": True,
        "out_of_sample": {
            "sharpe": 0.8,
            "total_return": 0.20,
            "max_drawdown": -0.20,
        },
        "benchmarks": {
            "buy_and_hold": {
                "total_return": 0.15,
                "max_drawdown": -0.40,
            }
        },
        "cost_stress": {"2x": {"total_return": 0.10}},
        "deflated_sharpe_probability": 0.95,
        "monte_carlo": {"loss_probability": 0.10},
    }


def test_verdict_rejects_failed_integrity_gate() -> None:
    evidence = _evidence()
    evidence["lookahead_consistent"] = False

    verdict = evaluate_research_verdict(evidence)

    assert verdict["status"] == "rejected"
    assert "lookahead" in " ".join(verdict["failed_gates"]).lower()


def test_verdict_marks_weak_robustness_as_fragile() -> None:
    evidence = _evidence()
    evidence["deflated_sharpe_probability"] = 0.70

    verdict = evaluate_research_verdict(evidence)

    assert verdict["status"] == "fragile"
    assert verdict["live_ready"] is False


def test_verdict_only_calls_passing_result_a_paper_research_candidate() -> None:
    verdict = evaluate_research_verdict(_evidence())

    assert verdict["status"] == "paper-research-candidate"
    assert verdict["live_ready"] is False
    assert verdict["failed_gates"] == []
