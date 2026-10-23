"""Central forecast authority: immutable, permissioned and event-clustered."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from qt.contracts import ForecastRecord
from qt.forecasting import ForecastRepository
from qt.forecasting.repository import forecast_records, forecast_resolution_events
from qt.persistence import Conflict, Repository, create_engine, migrate

_NOW = 1_700_000_000_000_000_000
_HASH = "a" * 64


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setattr("qt.forecasting.repository.time.time_ns", lambda: _NOW)
    monkeypatch.setattr("qt.agent_hub.research.time.time", lambda: _NOW / 1e9)
    engine = create_engine("sqlite:///:memory:", allow_sqlite=True)
    migrate(engine)
    repository = ForecastRepository(Repository(engine))
    yield repository
    engine.dispose()


def forecast(identity="one", **extra):
    return ForecastRecord(**{
        "forecast_id": identity, "event_id": "event-" + identity,
        "event_cluster_id": "cluster-" + identity, "market_id": "market-" + identity,
        "outcome_id": "yes", "probability": "0.8", "market_mid": "0.5",
        "decision_at_ns": _NOW - 1_000_000_000_000,
        "horizon_end_ns": _NOW - 100_000_000_000,
        "model_provider": "fixture", "model_id": "frozen-model", "prompt_hash": _HASH,
        "rules_hash": _HASH, "prediction_mode": "blind", **extra,
    })


def resolve(repo, identity="one", **extra):
    return repo.resolve("owner", identity, {
        "resolution_id": "resolution-" + identity, "status": "resolved", "outcome": 1,
        "resolved_at_ns": _NOW - 10_000_000_000, "observed_at_ns": _NOW,
        "source": "fixture-source", "source_hash": _HASH, "rules_hash": _HASH,
        "verified": True, **extra,
    }, actor_id="source-ingestor", permissions={"forecast:resolve"})


def test_zero_probability_missing_baseline_and_paired_coverage(repo):
    first = repo.record("owner", forecast(probability="0", market_mid=None,
                                          market_probability_missing_reason="not_observed"))
    assert first["payload"]["probability"] == "0"
    resolve(repo, outcome=0)
    repo.record("owner", forecast("paired"))
    resolve(repo, "paired")
    report = repo.score("owner")
    assert report["samples"] == 2
    assert report["brier_score"] == pytest.approx(0.02)
    assert report["paired_comparison_samples"] == 1
    assert report["market_baseline_coverage"] == 0.5
    assert report["brier_skill"] == pytest.approx(0.84)
    assert report["independent_event_clusters"] == 2
    assert report["live_ready"] is False


def test_one_hundred_repeated_forecasts_form_one_event_cohort(repo):
    for index in range(100):
        identity = f"forecast-{index:03d}"
        repo.record("owner", forecast(identity, event_id="same-event", event_cluster_id="same-cluster",
                                      decision_at_ns=_NOW - 1_000_000_000_000 + index * 1_000_000_000,
                                      probability="0.2" if index == 0 else "1"))
        resolve(repo, identity)
    report = repo.score("owner")
    assert report["samples"] == 1
    assert report["independent_event_clusters"] == 1
    assert report["counts"]["duplicate_cohort_forecasts"] == 99
    assert report["brier_score"] == pytest.approx(0.64)


def test_cohort_selection_does_not_depend_on_resolution_or_baseline(repo):
    repo.record("owner", forecast("early", event_id="same-event", event_cluster_id="same-cluster",
                                  market_mid=None, market_probability_missing_reason="no_quote"))
    repo.record("owner", forecast("later", event_id="same-event", event_cluster_id="same-cluster",
                                  decision_at_ns=_NOW - 900_000_000_000))
    resolve(repo, "later")
    assert repo.score("owner")["samples"] == 0
    resolve(repo, "early")
    assert repo.score("owner")["paired_comparison_samples"] == 0


def test_301_resolved_clusters_are_not_truncated_by_artifact_list_limit(repo):
    for index in range(301):
        identity = f"sample-{index:03d}"
        repo.record("owner", forecast(identity))
        resolve(repo, identity)
    report = repo.score("owner")
    assert report["samples"] == 301
    assert report["independent_event_clusters"] == 301
    assert report["paired_event_clusters"] == 301
    assert report["cluster_brier_skill"] == pytest.approx(0.84)
    assert report["cluster_brier_skill_95pct"] == pytest.approx([0.84, 0.84])


def test_same_event_multiple_horizons_and_outcomes_are_not_independent(repo):
    for index in range(3):
        identity = f"same-event-{index}"
        repo.record("owner", forecast(identity, event_id="same-event", event_cluster_id="shared",
                                      outcome_id="outcome-" + str(index)))
        resolve(repo, identity)
    report = repo.score("owner")
    assert report["samples"] == 3
    assert report["independent_event_clusters"] == 1
    assert report["paired_event_clusters"] == 1
    assert report["cluster_brier_skill_95pct"] is None
    assert report["effective_sample_size"] is None


def test_permissioned_append_only_resolution_corrections_and_special_payout(repo):
    repo.record("owner", forecast())
    first = resolve(repo)
    assert resolve(repo) == first
    resolve(repo, resolution_id="dispute", status="disputed")
    assert repo.score("owner")["counts"]["disputed_forecasts"] == 1
    resolve(repo, resolution_id="special-final", outcome=None, payout="0.5")
    report = repo.score("owner")
    assert report["samples"] == 0 and report["counts"]["non_binary_resolutions"] == 1
    history = repo.history("owner", "one")
    assert [row["revision"] for row in history] == [1, 2, 3]
    assert history[0]["payload"]["outcome"] == 1
    assert history[-1]["payload"]["payout"] == "0.5"
    assert history[-1]["actor_id"] == "source-ingestor"
    assert history[-1]["payload"]["rules_hash"] == _HASH


def test_unresolved_canceled_and_disputed_counts_are_complete(repo):
    for identity in ("open", "canceled", "disputed"):
        repo.record("owner", forecast(identity))
    resolve(repo, "canceled", status="canceled")
    resolve(repo, "disputed", status="disputed")
    counts = repo.score("owner")["counts"]
    assert counts["unresolved_forecasts"] == 1
    assert counts["canceled_forecasts"] == 1
    assert counts["disputed_forecasts"] == 1


@pytest.mark.parametrize("permissions", [set(), {"owner"}, "forecast:resolve", None])
def test_owner_or_unprivileged_actor_cannot_supply_scored_outcomes(repo, permissions):
    repo.record("owner", forecast())
    with pytest.raises(PermissionError, match="source_resolution_permission"):
        repo.resolve("owner", "one", {}, actor_id="owner", permissions=permissions)
    assert repo.history("owner", "one") == []


@pytest.mark.parametrize("extra", [
    {"verified": False}, {"source_hash": "not-a-sha"}, {"rules_hash": "b" * 64},
    {"resolved_at_ns": _NOW + 1}, {"resolved_at_ns": _NOW - 2_000_000_000_000},
    {"resolved_at_ns": float(_NOW)}, {"outcome": True}, {"outcome": 0.5},
    {"outcome": 1, "payout": "0.5"}, {"outcome": None, "payout": 0.5},
    {"payout": "NaN"}, {"payout": "garbage"}, {"source": ""},
    {"redeemed_at_ns": _NOW - 11_000_000_000}, {"unrecognized": "field"},
])
def test_resolution_evidence_timestamps_and_probabilities_are_validated(repo, extra):
    repo.record("owner", forecast())
    with pytest.raises(ValueError):
        resolve(repo, **extra)
    assert repo.history("owner", "one") == []


def test_immutable_id_conflicts_and_tenant_isolation(repo):
    first = repo.record("owner", forecast())
    assert repo.record("owner", forecast()) == first
    with pytest.raises(Conflict, match="forecast_identity_conflict"):
        repo.record("owner", forecast(probability="0.3"))
    resolve(repo)
    with pytest.raises(Conflict, match="resolution_identity_conflict"):
        resolve(repo, outcome=0)
    assert repo.score("other")["samples"] == 0
    with pytest.raises(KeyError, match="forecast_not_found"):
        repo.history("other", "one")


def test_concurrent_duplicate_resolution_only_creates_one_revision(repo):
    repo.record("owner", forecast())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: resolve(repo), range(8)))
    assert all(result == results[0] for result in results)
    assert len(repo.history("owner", "one")) == 1


def test_forecast_and_resolution_tampering_fail_closed(repo):
    repo.record("owner", forecast())
    result = resolve(repo)
    with repo.repo.transaction() as connection:
        connection.execute(update(forecast_resolution_events).where(
            forecast_resolution_events.c.resolution_id == result["resolution_id"],
        ).values(payload={**result["payload"], "outcome": 0}))
    with pytest.raises(Conflict, match="resolution_integrity_failure"):
        repo.score("owner")
    with pytest.raises(Conflict, match="resolution_integrity_failure"):
        repo.history("owner", "one")
    with repo.repo.transaction() as connection:
        connection.execute(update(forecast_records).where(
            forecast_records.c.forecast_id == "one",
        ).values(sha256="b" * 64))
    with pytest.raises(Conflict, match="forecast_integrity_failure"):
        repo.get("owner", "one")
