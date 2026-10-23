"""Forecast evidence must remain immutable, time causal and honestly sampled."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from qt.agent_hub.forecast_ledger import ForecastLedger, net_edge_payload

_NOW = 1_700_000_000.0


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ForecastLedger:
    monkeypatch.setattr("qt.agent_hub.forecast_ledger.time.time", lambda: _NOW)
    return ForecastLedger(tmp_path / "forecasts.sqlite3")


def record(ledger: ForecastLedger, name: str = "first", **extra) -> dict:
    return ledger.record_forecast(
        "owner",
        {"forecast_id": name, "market_id": name, "p_model": 0.8,
         "decision_time": _NOW - 200, **extra},
    )


def settle(ledger: ForecastLedger, name: str = "first", **extra) -> dict:
    return ledger.resolve(
        "owner", name,
        {"outcome": 1, "settle_ts": _NOW - 10, "settle_source": "verified-fixture", **extra},
    )


def test_zero_probability_survives_aliases_and_idempotent_retry(ledger: ForecastLedger) -> None:
    first = record(ledger, p_model=0, probability=0, p_market_mid=0)
    assert first["p_model"] == 0
    assert first["p_market_mid"] == 0
    assert record(ledger, p_model=0, probability=0, p_market_mid=0) == first
    settle(ledger, outcome=0)
    score = ledger.score("owner")
    assert score["brier_score"] == 0
    assert score["market_baseline_brier"] == 0
    assert score["brier_skill"] is None
    assert score["paired_comparison_samples"] == 1
    assert score["live_ready"] is False


def test_missing_market_probability_is_not_a_half_probability(ledger: ForecastLedger) -> None:
    record(ledger)
    settle(ledger)
    score = ledger.score("owner")
    assert score["samples"] == 1
    assert score["brier_score"] == pytest.approx(0.04)
    assert score["market_baseline_brier"] is None
    assert score["brier_skill"] is None
    assert score["paired_comparison_samples"] == 0
    assert score["comparison_coverage"] == 0
    assert score["market_comparison_status"] == "missing_market_baseline"


def test_partial_baseline_coverage_uses_only_paired_forecasts(ledger: ForecastLedger) -> None:
    record(ledger, p_market_mid=0.5)
    settle(ledger)
    record(ledger, "second", p_model=0)
    settle(ledger, "second")
    score = ledger.score("owner")
    assert score["brier_score"] == pytest.approx(0.52)
    assert score["paired_model_brier"] == pytest.approx(0.04)
    assert score["market_baseline_brier"] == pytest.approx(0.25)
    assert score["brier_skill"] == pytest.approx(0.84)
    assert score["paired_comparison_samples"] == 1
    assert score["comparison_coverage"] == 0.5


def test_one_hundred_repeated_forecasts_do_not_create_independent_events(ledger: ForecastLedger) -> None:
    for index in range(100):
        record(
            ledger, f"prediction-{index:03d}", market_id="same-market",
            decision_time=_NOW - 200 + index, p_model=0.2 if index == 0 else 1,
        )
        settle(ledger, f"prediction-{index:03d}")
    score = ledger.score("owner")
    assert score["samples"] == 1
    assert score["scored_observations"] == 1
    assert score["independent_event_clusters"] == 1
    assert score["counts"]["duplicate_cohort_forecasts"] == 99
    assert score["brier_score"] == pytest.approx(0.64)


def test_outcomes_and_horizons_do_not_claim_independent_event_samples(ledger: ForecastLedger) -> None:
    for name, horizon, outcome_id in (("a", 60, "yes"), ("b", 600, "yes"), ("c", 60, "other")):
        record(ledger, name, market_id="one-event", horizon_s=horizon, outcome_id=outcome_id)
        settle(ledger, name)
    score = ledger.score("owner")
    assert score["samples"] == 3
    assert score["independent_event_clusters"] == 1
    assert score["effective_sample_size"] is None
    assert score["selection_policy"] == "first_forecast_per_event_outcome_horizon"


def test_related_markets_use_explicit_event_and_family_identity(ledger: ForecastLedger) -> None:
    record(ledger, "a", event_id="election", event_family_id="common-shock")
    record(ledger, "b", event_id="election", event_family_id="common-shock")
    record(ledger, "c", event_id="another-event", event_family_id="common-shock")
    for name in ("a", "b", "c"):
        settle(ledger, name)
    score = ledger.score("owner")
    assert score["samples"] == 2
    assert score["independent_event_clusters"] == 1


def test_different_rounds_are_explicitly_different_events(ledger: ForecastLedger) -> None:
    for index in range(2):
        record(ledger, str(index), market_id="BTCUSDT", round_start_unix=int(_NOW - 1000 + index * 100))
        settle(ledger, str(index))
    assert ledger.score("owner")["independent_event_clusters"] == 2


def test_first_forecast_selection_cannot_depend_on_baseline_or_outcome(ledger: ForecastLedger) -> None:
    record(ledger, "first", market_id="one", p_market_mid=None)
    record(ledger, "later", market_id="one", decision_time=_NOW - 100, p_market_mid=0.5)
    settle(ledger, "later")
    score = ledger.score("owner")
    assert score["samples"] == 0
    assert score["counts"]["unresolved_forecasts"] == 1
    settle(ledger, "first")
    score = ledger.score("owner")
    assert score["samples"] == 1
    assert score["paired_comparison_samples"] == 0


def test_empty_unresolved_canceled_disputed_and_special_payouts_are_visible(ledger: ForecastLedger) -> None:
    assert ledger.score("owner")["counts"]["total_forecasts"] == 0
    for name in ("unresolved", "canceled", "disputed", "half", "no-model"):
        record(ledger, name, p_model=None if name == "no-model" else 0.8)
    ledger.resolve("owner", "canceled", {"status": "canceled"})
    settle(ledger, "disputed", dispute=True)
    settle(ledger, "half", outcome=None, payout=0.5)
    settle(ledger, "no-model")
    score = ledger.score("owner")
    assert score["samples"] == 0
    assert score["counts"] == {
        "total_forecasts": 5, "unresolved_forecasts": 1,
        "canceled_forecasts": 1, "disputed_forecasts": 1,
        "non_binary_resolutions": 1, "missing_model_probability": 1,
        "invalid_observations": 0, "duplicate_cohort_forecasts": 0,
    }
    assert ledger.resolution_history("owner", "half")[0]["payout"] == 0.5


def test_resolution_revisions_preserve_history_and_latest_state(ledger: ForecastLedger) -> None:
    record(ledger)
    first = settle(ledger, resolution_id="source-v1")
    dispute = settle(ledger, resolution_id="dispute", dispute=True)
    assert ledger.score("owner")["samples"] == 0
    final = settle(ledger, resolution_id="source-v2", outcome=0, payout=0)
    assert [row["revision"] for row in ledger.resolution_history("owner", "first")] == [1, 2, 3]
    assert first["outcome"] == 1
    assert dispute["status"] == "disputed"
    assert final["outcome"] == 0
    assert ledger.score("owner")["brier_score"] == pytest.approx(0.64)
    assert settle(ledger, resolution_id="source-v1") == first
    assert len(ledger.resolution_history("owner", "first")) == 3
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT outcome FROM resolutions").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE resolution_events SET outcome=1")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM resolution_events")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE forecasts SET p_model=1")


def test_implicit_resolution_retries_do_not_append_new_revisions(ledger: ForecastLedger) -> None:
    record(ledger)
    first = ledger.resolve("owner", "first", {"outcome": 1})
    assert ledger.resolve("owner", "first", {"outcome": 1}) == first
    assert len(ledger.resolution_history("owner", "first")) == 1


def test_same_identity_different_payload_or_tenant_is_rejected(ledger: ForecastLedger) -> None:
    record(ledger)
    with pytest.raises(ValueError, match="forecast_id_conflict"):
        record(ledger, p_model=0.7)
    with pytest.raises(ValueError, match="forecast_id_conflict"):
        ledger.record_forecast("other-tenant", {"forecast_id": "first"})
    settle(ledger, resolution_id="r1")
    with pytest.raises(ValueError, match="resolution_id_conflict"):
        settle(ledger, resolution_id="r1", outcome=0)
    assert ledger.resolution_history("other-tenant", "first") == []
    assert ledger.lookup("other-tenant", "first") == []
    with pytest.raises(KeyError, match="forecast_not_found"):
        ledger.resolve("other-tenant", "first", {"outcome": 1})


@pytest.mark.parametrize("extra", [
    {"p_model": -0.01}, {"p_model": 1.01}, {"p_model": float("nan")},
    {"p_model": True}, {"p_market_mid": float("inf")}, {"p_market_mid": 40_000},
    {"p_model": 0, "probability": 1}, {"decision_time": 0},
    {"decision_time": float("nan")}, {"decision_time": _NOW + 1},
    {"knowledge_time": _NOW - 100}, {"horizon_s": 0},
    {"round_start_unix": True}, {"round_start_unix": int(_NOW + 1)},
    {"best_bid": 0.8, "best_ask": 0.7}, {"best_bid": 40_000},
])
def test_forecast_probabilities_and_causal_timestamps_are_validated(ledger: ForecastLedger, extra: dict) -> None:
    with pytest.raises(ValueError):
        record(ledger, **extra)
    assert ledger.lookup("owner", "first") == []


@pytest.mark.parametrize("extra", [
    {"outcome": 0.5}, {"outcome": True}, {"payout": -1}, {"payout": float("nan")},
    {"outcome": 1, "payout": 0.5}, {"settle_ts": _NOW - 200},
    {"settle_ts": _NOW + 1}, {"redeemed_ts": _NOW - 100},
    {"status": "resolved", "dispute": True}, {"dispute": 1},
])
def test_resolution_values_and_ordering_are_validated(ledger: ForecastLedger, extra: dict) -> None:
    record(ledger)
    with pytest.raises(ValueError):
        settle(ledger, **extra)
    assert ledger.resolution_history("owner", "first") == []


def test_concurrent_retries_create_one_forecast_and_resolution(ledger: ForecastLedger) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: record(ledger), range(8)))
    assert all(row == rows[0] for row in rows)
    with ThreadPoolExecutor(max_workers=4) as pool:
        resolutions = list(pool.map(lambda _: settle(ledger, resolution_id="one-resolution"), range(8)))
    assert all(row == resolutions[0] for row in resolutions)
    assert len(ledger.lookup("owner", "first")) == 1
    assert len(ledger.resolution_history("owner", "first")) == 1


def test_migration_preserves_old_rows_and_is_repeatable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("qt.agent_hub.forecast_ledger.time.time", lambda: _NOW)
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE forecasts (
                forecast_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, run_id TEXT,
                market_id TEXT NOT NULL, token_id TEXT, round_start_unix INTEGER,
                p_model REAL, p_market_mid REAL, best_bid REAL, best_ask REAL,
                fee_at_p REAL, horizon_s INTEGER, decision_time REAL NOT NULL,
                knowledge_time REAL, evidence_receipt TEXT, net_edge_reason TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE resolutions (
                forecast_id TEXT PRIMARY KEY, outcome INTEGER, settle_ts REAL,
                settle_source TEXT, payout REAL, redeemed_ts REAL,
                dispute INTEGER NOT NULL DEFAULT 0
            );
        """)
        db.execute(
            "INSERT INTO forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy", "owner", None, "legacy-market", None, None, 0.8, None,
             None, None, None, None, _NOW - 200, None, None, "old-reason", _NOW - 200),
        )
        db.execute("INSERT INTO resolutions VALUES(?,?,?,?,?,?,?)", ("legacy", 1, _NOW - 10, "old-source", 1, None, 0))
    ledger = ForecastLedger(path)
    assert ledger.score("owner")["samples"] == 1
    old = ledger.resolution_history("owner", "legacy")[0]
    assert old["resolution_id"] == "legacy:legacy"
    assert old["settle_source"] == "old-source"
    ledger.resolve("owner", "legacy", {"outcome": 0, "settle_ts": _NOW - 10})
    reopened = ForecastLedger(path)
    assert len(reopened.resolution_history("owner", "legacy")) == 2
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT outcome FROM resolutions_legacy").fetchone()[0] == 1
        assert db.execute("SELECT outcome FROM resolutions").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM resolutions_legacy")


def test_net_edge_preserves_zero_and_subtracts_per_share_costs() -> None:
    assert net_edge_payload({"estimated_probability": 0, "best_ask": 0.5, "fee_at_p": 0})["net_edge"] == -0.5
    assert net_edge_payload({"estimated_probability": 0.8, "best_ask": 0.5, "fee_at_p": 0.01,
                             "slippage_per_share": 0.02, "capital_cost_per_share": 0.03})["net_edge"] == pytest.approx(0.24)
    assert net_edge_payload({"estimated_probability": 0.8, "best_ask": 0.5})["net_edge"] is None
    with pytest.raises(ValueError):
        net_edge_payload({"estimated_probability": float("nan")})
