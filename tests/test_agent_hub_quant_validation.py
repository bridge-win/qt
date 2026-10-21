"""Adversarial validation of actual native observations and fold resampling."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from qt.agent_hub.quant_validation import (
    annualized_return,
    audit_native_timeline,
    fold_bootstrap,
)


def native_evidence() -> tuple[dict, dict]:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    close = [(start + timedelta(days=index)).isoformat() for index in (5, 6, 7)]
    values = [1000.0, 1010.0, 1000.0, 1030.0]
    return (
        {
            "series": {
                "equity": dict(zip([start.isoformat(), *close], values, strict=True)),
                "returns": {
                    timestamp: values[index + 1] / values[index] - 1
                    for index, timestamp in enumerate(close)
                },
            },
            "decision_traces": [
                {"timestamp": timestamp, "observed_rows": index + 5}
                for index, timestamp in enumerate(close)
            ],
        },
        {
            "start": (start + timedelta(days=4)).isoformat(),
            "end": (start + timedelta(days=6)).isoformat(),
            "snapshot_start_ms": int(start.timestamp() * 1000),
        },
    )


def test_native_audit_checks_actual_closed_bar_coverage_and_causal_rows() -> None:
    evidence, window = native_evidence()
    result = audit_native_timeline(evidence, **window)
    assert result["status"] == "verified"
    assert result["bars"] == 3
    assert result["annualization"] == "decision_window_only_excludes_warmup"


@pytest.mark.parametrize(
    "attack", ["future_row", "missing_return", "reordered_return", "wrong_pnl", "missing_trace"]
)
def test_window_metadata_cannot_attest_bad_native_observations(attack: str) -> None:
    original, window = native_evidence()
    evidence = copy.deepcopy(original)
    if attack == "future_row":
        evidence["decision_traces"][0]["observed_rows"] += 1
    elif attack == "missing_return":
        evidence["series"]["returns"].pop(next(iter(evidence["series"]["returns"])))
    elif attack == "reordered_return":
        evidence["series"]["returns"] = dict(reversed(list(evidence["series"]["returns"].items())))
    elif attack == "wrong_pnl":
        evidence["series"]["returns"][next(iter(evidence["series"]["returns"]))] = 0.9
    else:
        evidence["decision_traces"].pop()
    with pytest.raises(ValueError):
        audit_native_timeline(evidence, **window)


def test_cagr_uses_decision_window_instead_of_warmup_calendar() -> None:
    assert annualized_return(0.1, 365) == pytest.approx(0.1000718114)
    assert annualized_return(0.1, 30) > 2
    assert annualized_return(-1, 30) == -1
    assert annualized_return(100000, 1) is None


def test_bootstrap_never_mixes_reset_folds_and_is_reproducible() -> None:
    # Each fold is constant, so circular resampling within folds must preserve
    # its exact return. Pooling folds would fabricate a broad distribution.
    folds = [[0.01] * 20, [-0.005] * 20]
    result = fold_bootstrap(folds, block_sizes=(7, 14), simulations=100, seed=99)
    assert result == fold_bootstrap(folds, block_sizes=(7, 14), simulations=100, seed=99)
    expected = (1.01**20) * (0.995**20) - 1
    assert result["cross_fold_blocks"] is False
    assert result["fold_bars"] == [20, 20]
    for sensitivity in result["sensitivity"]:
        assert sensitivity["return_p05"] == pytest.approx(expected)
        assert sensitivity["return_p95"] == pytest.approx(expected)


def test_bootstrap_uncertainty_can_reject_positive_realized_return() -> None:
    folds = [[0.0] * 19 + [0.5], [0.0] * 19 + [-0.01]]
    result = fold_bootstrap(folds, simulations=1000, seed=7)
    assert result["worst_return_p05"] <= 0
    assert result["worst_loss_probability"] > 0.05


@pytest.mark.parametrize("folds", [[], [[]], [[float("nan")]], [[-1]]])
def test_bootstrap_fails_closed_on_invalid_observations(folds: list) -> None:
    with pytest.raises(ValueError):
        fold_bootstrap(folds)
