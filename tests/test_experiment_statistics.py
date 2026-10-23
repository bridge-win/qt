"""Tests distinguish valid mathematical results from unsupported release claims."""

from __future__ import annotations

import json
import sqlite3
from math import sqrt

import numpy as np
import pytest
from sqlalchemy import select

from qt.agent_hub.trial_ledger import TrialLedger
from qt.experiments.metrics import DAY_NS, net_equity_metrics
from qt.experiments.registry import ExperimentRegistry
from qt.experiments.splits import LabelInterval, cpcv_splits, purge_training, walk_forward_splits
from qt.experiments.statistics import (
    CandidateSharpe,
    block_bootstrap_mean,
    deflated_sharpe,
    holm_adjust,
    probability_of_backtest_overfitting,
)
from qt.persistence.database import create_engine, migrate
from qt.persistence.repository import Conflict
from qt.persistence.schema import events


@pytest.fixture
def registry():
    engine = create_engine("sqlite:///:memory:", allow_sqlite=True)
    migrate(engine)
    yield ExperimentRegistry(engine)
    engine.dispose()


def campaign(registry, identity="campaign", start=100, end=200):
    return registry.register_campaign(
        "tenant", identity, dataset_id="dataset-content-hash",
        holdout_start_ns=start, holdout_end_ns=end, protocol={"primary": "net_mean"},
    )


def test_actual_cpcv_splits_have_fifteen_splits_five_paths():
    labels = [LabelInterval(i * 10, i * 10 + 2) for i in range(60)]
    plan = cpcv_splits(labels, groups=6, test_groups=2)
    assert len(plan.splits) == 15
    assert len(plan.paths) == 5
    assert plan.independent_paths is False
    used = []
    for path in plan.paths:
        assert [segment.group for segment in path] == list(range(6))
        assert [i for segment in path for i in segment.test_indices] == list(range(60))
        for segment in path:
            used.append((segment.group, segment.split_id))
            split = plan.splits[segment.split_id]
            assert set(segment.test_indices) <= set(split.test_indices)
            assert not set(split.train_indices) & set(split.test_indices)
    assert len(used) == len(set(used)) == 30


def test_purge_uses_label_information_and_embargo_not_warmup():
    intervals = [LabelInterval(0, 10), LabelInterval(10, 15), LabelInterval(16, 18),
                 LabelInterval(19, 20), LabelInterval(25, 30)]
    # Endpoint 10 overlaps the test; starts 16..18 lie in a 3-unit post-label embargo.
    assert purge_training(intervals, [0, 2, 3, 4], [1], embargo=3) == (3, 4)


def test_walk_forward_excludes_future_label_overlap():
    labels = [LabelInterval(i, i + 2) for i in range(20)]
    splits = walk_forward_splits(labels, train_size=8, test_size=4, expanding=True)
    assert len(splits) == 3
    for split in splits:
        assert max(labels[i].end for i in split.train_indices) < min(labels[i].start for i in split.test_indices)
    assert not set(splits[0].train_indices) & {6, 7}
    with pytest.raises(ValueError, match="overlapping_tests"):
        walk_forward_splits(labels, train_size=8, test_size=4, step=1)


def test_dsr_requires_complete_comparable_trials_and_actual_variance():
    returns = [-0.01, 0.02] * 100
    candidates = [CandidateSharpe("a", 0, "same-clock-cost-split"),
                  CandidateSharpe("b", 0.1, "same-clock-cost-split"),
                  CandidateSharpe("c", 0.2, "same-clock-cost-split")]
    result = deflated_sharpe(returns, candidates, expected_trial_ids=["a", "b", "c"], comparison_key="same-clock-cost-split")
    assert result["status"] == "computed"
    assert result["trial_sharpe_variance"] == pytest.approx(0.01)
    assert result["benchmark_sharpe_per_observation"] == pytest.approx(0.0853, abs=0.0001)
    assert 0.95 < result["value"] <= 1
    incomplete = deflated_sharpe(returns, candidates[:-1], expected_trial_ids=["a", "b", "c"], comparison_key="same-clock-cost-split")
    assert incomplete["status"] == "not_applicable"
    wrong_clock = deflated_sharpe(returns, candidates, expected_trial_ids=["a", "b", "c"], comparison_key="annualized")
    assert wrong_clock["status"] == "not_applicable"
    with pytest.raises(ValueError, match="duplicate_trial"):
        deflated_sharpe(returns, candidates + candidates[:1], expected_trial_ids=["a", "b", "c"], comparison_key="same-clock-cost-split")


def test_pbo_does_not_accept_candidate_counts_as_evidence():
    missing = probability_of_backtest_overfitting(None, candidate_ids=["a", "b"], expected_trial_ids=["a", "b"])
    assert missing["status"] == "not_applicable" and missing["value"] is None
    matrix = [[0.1, -0.1], [0.1, -0.1], [-0.1, 0.1], [-0.1, 0.1]]
    result = probability_of_backtest_overfitting(matrix, candidate_ids=["a", "b"], expected_trial_ids=["a", "b"])
    assert result["partitions"] == 6
    assert result["value"] == 1
    result = probability_of_backtest_overfitting(matrix, candidate_ids=["a", "b"], expected_trial_ids=["a", "b", "failed-c"])
    assert result["status"] == "not_applicable"


def test_holm_and_seeded_dependence_preserving_bootstrap():
    assert holm_adjust([0.03, 0.01, 0.04]) == pytest.approx((0.06, 0.03, 0.06))
    series = np.repeat([-0.02, 0.03, 0.01, -0.01], 20).tolist()
    first = block_bootstrap_mean(series, block_size=10, resamples=1000, seed=42)
    assert first == block_bootstrap_mean(series, block_size=10, resamples=1000, seed=42)
    assert first["lower"] < first["value"] < first["upper"]
    assert block_bootstrap_mean(series[:4], block_size=3)["status"] == "not_applicable"


def test_marked_equity_flows_drawdown_and_undefined_ratios():
    report = net_equity_metrics([100, 120, 132], [0, DAY_NS, 2 * DAY_NS], external_flows=[0, 10, 0])
    metrics = report["metrics"]
    assert metrics["cumulative_return"]["value"] == pytest.approx(0.21)
    assert metrics["max_drawdown"]["value"] == 0
    assert metrics["drawdown_duration_days"]["value"] == 0
    assert metrics["sortino"]["value"] is None
    assert metrics["sortino"]["reason"] == "no_downside_observations"
    assert metrics["cvar_99"]["reliable_tail_sample"] is False
    assert report["effective_observations"] is None
    json.dumps(report, allow_nan=False)


def test_sortino_uses_lower_partial_moment_and_duration_includes_recovery():
    report = net_equity_metrics([100, 120, 108, 121], [0, DAY_NS, 2 * DAY_NS, 3 * DAY_NS])
    assert report["metrics"]["max_drawdown"]["value"] == pytest.approx(0.1)
    assert report["metrics"]["drawdown_duration_days"]["value"] == 2
    short = net_equity_metrics([100, 120, 108], [0, DAY_NS, 2 * DAY_NS])
    expected = 0.05 / sqrt(0.01 / 2) * sqrt(365)
    assert short["metrics"]["sortino"]["value"] == pytest.approx(expected)


def test_net_equity_validates_clock_and_reports_missing_day():
    with pytest.raises(ValueError, match="integer_nanoseconds"):
        net_equity_metrics([1, 2], [0.0, 1.0])
    report = net_equity_metrics([100, 110, 120], [0, DAY_NS, 3 * DAY_NS])
    assert report["metrics"]["sharpe"]["value"] is None
    assert report["coverage_gaps"] == 1


def test_campaign_and_trial_identities_are_immutable_and_all_failures_count(registry):
    first = campaign(registry)
    assert campaign(registry) == first
    row = registry.register_trial("tenant", "campaign", "a", {"params": {"x": 1}})
    assert registry.register_trial("tenant", "campaign", "a", {"params": {"x": 1}}) == row
    with pytest.raises(Conflict, match="identity_conflict"):
        registry.register_trial("tenant", "campaign", "a", {"params": {"x": 2}})
    for identity, status in [("a", "failed"), ("b", "aborted"), ("c", "completed")]:
        if identity != "a":
            registry.register_trial("tenant", "campaign", identity, {"params": {"x": identity}})
        registry.finish_trial("tenant", identity, status=status, result={"reason": status})
    assert len(registry.trial_population("tenant", "campaign")) == 3
    with pytest.raises(Conflict, match="immutable"):
        registry.finish_trial("tenant", "a", status="completed", result={"return": 1})


def test_holdout_cannot_be_bypassed_with_new_trial_or_campaign(registry):
    campaign(registry)
    registry.register_trial("tenant", "campaign", "a", {"params": {"x": 1}})
    registry.finish_trial("tenant", "a", status="completed", result={"development": True})
    with pytest.raises(PermissionError):
        registry.unlock_holdout("tenant", "campaign", actor_id="proposer", permissions=frozenset(), reason="inspect")
    grant = registry.unlock_holdout("tenant", "campaign", actor_id="validator", permissions=frozenset({"holdout:unlock"}), reason="frozen final evaluation")
    with pytest.raises(PermissionError, match="already_unlocked"):
        registry.unlock_holdout("tenant", "campaign", actor_id="validator", permissions=frozenset({"holdout:unlock"}), reason="again")
    with pytest.raises(Conflict, match="frozen_after"):
        registry.register_trial("tenant", "campaign", "new-trial", {"params": {"x": 2}})
    with pytest.raises(Conflict, match="already_owned"):
        campaign(registry, "new-campaign", start=120, end=220)
    with pytest.raises(PermissionError, match="overlaps_protected"):
        registry.guard_development_read("tenant", dataset_id="dataset-content-hash", start_ns=99, end_ns=101)
    registry.guard_development_read("tenant", dataset_id="dataset-content-hash", start_ns=0, end_ns=100)
    assert registry.authorize_holdout_read("tenant", "campaign", actor_id="validator", grant_id=grant["grant_id"], permissions=frozenset({"holdout:read"}))["campaign_id"] == "campaign"
    with pytest.raises(PermissionError):
        registry.authorize_holdout_read("tenant", "campaign", actor_id="proposer", grant_id=grant["grant_id"], permissions=frozenset({"holdout:read"}))
    with registry.engine.begin() as db:
        kinds = db.execute(select(events.c.event_type)).scalars().all()
    assert "holdout_read_denied" in kinds and "holdout_unlock_denied" in kinds


def test_holdout_requires_terminal_search_population(registry):
    campaign(registry)
    with pytest.raises(PermissionError, match="not_terminal"):
        registry.unlock_holdout("tenant", "campaign", actor_id="validator", permissions=frozenset({"holdout:unlock"}), reason="final")
    registry.register_trial("tenant", "campaign", "pending", {"x": 1})
    with pytest.raises(PermissionError, match="not_terminal"):
        registry.unlock_holdout("tenant", "campaign", actor_id="validator", permissions=frozenset({"holdout:unlock"}), reason="final")


def test_legacy_trials_report_truthful_statistics_and_do_not_silently_reuse_ids(tmp_path):
    ledger = TrialLedger(tmp_path / "trials.db")
    row = ledger.record("tenant", {"trial_id": "a", "snapshot_id": "s", "oos_sharpe": 0.1})
    assert ledger.record("tenant", {"trial_id": "a", "snapshot_id": "s", "oos_sharpe": 0.1})["created_at"] == row["created_at"]
    with pytest.raises(ValueError, match="payload_conflict"):
        ledger.record("tenant", {"trial_id": "a", "snapshot_id": "s", "oos_sharpe": 100})
    ledger.record("tenant", {"trial_id": "b", "snapshot_id": "s", "status": "failed"})
    summary = ledger.summary("tenant", "s", [0.1, 0.2])
    assert summary["walk_forward"]["folds"] == 2
    assert summary["cpcv"]["paths"] == 0
    assert summary["dsr"]["status"] == summary["pbo"]["status"] == "not_applicable"
    assert summary["search_accounting"]["global_selection_bias_corrected"] is False
    with pytest.raises(PermissionError):
        ledger.unlock_holdout("tenant", "a")
    ledger.unlock_holdout("tenant", "a", actor_id="validator", permissions=frozenset({"holdout:unlock"}), reason="frozen")
    with pytest.raises(PermissionError, match="already_unlocked"):
        ledger.unlock_holdout("tenant", "b", actor_id="validator", permissions=frozenset({"holdout:unlock"}), reason="again")
    with pytest.raises(ValueError, match="snapshot_frozen"):
        ledger.record("tenant", {"trial_id": "c", "snapshot_id": "s"})
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM holdout_audit").fetchone()[0] == 3
    assert ledger.summary("tenant", "s", [])["holdout"]["sealed"] is False
