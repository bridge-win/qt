from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qt.btc_alerts.__main__ import health
from qt.btc_alerts.api import build_alerts_router
from qt.btc_alerts.evaluation import confidence_interval, outcome, resample, trend_baseline
from qt.btc_alerts.models import Bar
from qt.btc_alerts.store import Store
from qt.workbench.api import WorkbenchSettings, create_workbench_app

from .conftest import NOW, packet


def client_for(root):
    app = FastAPI()
    app.include_router(build_alerts_router(root), prefix="/api/v3")
    return TestClient(app)


def test_unconfigured_host_is_explicit_and_does_not_create_fake_state():
    response = client_for(None).get("/api/v3/btc-alerts")
    assert response.status_code == 200
    assert response.json()["configured"] is False
    assert response.json()["live_execution_enabled"] is False


def test_policy_revision_conflict_and_threshold_version_force_shadow(store):
    client = client_for(store.path.parent)
    initial = client.get("/api/v3/btc-alerts").json()
    policy = initial["policy"]
    policy["mode"] = "notify"
    policy["thresholds"]["breakout_volume"] = 1.8
    response = client.put("/api/v3/btc-alerts/policy", json={"expected_revision": initial["revision"], "policy": policy})
    assert response.status_code == 200
    assert response.json()["policy"]["mode"] == "shadow"
    assert response.json()["strategy_version"] != initial["strategy_version"]
    response = client.put("/api/v3/btc-alerts/policy", json={"expected_revision": initial["revision"], "policy": policy})
    assert response.status_code == 409


def test_notify_and_test_require_real_readiness(store):
    client = client_for(store.path.parent)
    initial = client.get("/api/v3/btc-alerts").json()
    policy = initial["policy"] | {"mode": "notify"}
    assert client.put("/api/v3/btc-alerts/policy", json={"expected_revision": initial["revision"], "policy": policy}).status_code == 409
    assert client.post("/api/v3/btc-alerts/test-notification", json={"request_id": "fixture-request-id"}).status_code == 409


def test_test_request_is_idempotent_and_has_no_recipient_override(store):
    now = time.time()
    store.put("heartbeat", now)
    store.put("cycle_started", now)
    store.put("worker_status", {"at": now, "recipient_verified": True, "status": "watching"})
    client = client_for(store.path.parent)
    payload = {"request_id": "fixture-request-id"}
    first = client.post("/api/v3/btc-alerts/test-notification", json=payload)
    second = client.post("/api/v3/btc-alerts/test-notification", json=payload)
    assert first.status_code == 202
    assert first.json() == second.json()
    assert client.post("/api/v3/btc-alerts/test-notification", json=payload | {"chat_id": "other"}).status_code == 422
    assert client.get("/api/v3/btc-alerts/events/" + first.json()["event_id"]).json()["payload"]["is_test"] is True


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/api/v3/btc-alerts", None),
    ("GET", "/api/v3/btc-alerts/events/any", None),
    ("PUT", "/api/v3/btc-alerts/policy", {}),
    ("POST", "/api/v3/btc-alerts/test-notification", {"request_id": "fixture-request-id"}),
])
def test_existing_origin_auth_covers_every_alert_route(tmp_path, method, path, payload):
    app = create_workbench_app(WorkbenchSettings(
        parquet_root=tmp_path / "parquet", state_root=tmp_path / "research",
        btc_alerts_state_dir=tmp_path / "alerts",
        origin_client_id="fixture-origin-id", origin_client_secret="fixture-origin-secret",
    ))
    assert TestClient(app).request(method, path, json=payload).status_code == 401


def test_health_probe_is_read_only_and_rejects_dead_or_stalled_worker(tmp_path):
    root = tmp_path / "missing"
    assert not health(root)
    assert not root.exists()
    store = Store(root)
    store.put("heartbeat", time.time())
    store.put("cycle_started", time.time())
    assert health(root)
    store.put("cycle_started", time.time() - 241)
    assert not health(root)


def test_projection_does_not_renew_stale_worker_readiness(store):
    store.put("heartbeat", time.time() - 30)
    store.put("worker_status", {"status": "watching", "recipient_verified": True, "markets": {"spot": {"ready": True}}})
    value = client_for(store.path.parent).get("/api/v3/btc-alerts").json()
    assert not value["worker"]["alive"]
    assert not value["readiness"]["recipient"]
    assert value["worker"]["markets"] == {}


def future_bars(candidate):
    return [Bar(open_time=timestamp, open=candidate.reference_price, high=candidate.reference_price + 5,
                low=candidate.reference_price - 5, close=candidate.reference_price, volume=100)
            for timestamp in range(NOW + 900, NOW + 72 * 3600, 900)]


def test_ambiguous_same_bar_stop_and_target_uses_adverse_outcome(candidate, policy):
    rows = future_bars(candidate)
    rows[0] = rows[0].model_copy(update={
        "high": candidate.target_price + 1, "low": candidate.invalidation_price - 1,
    })
    result = outcome(candidate, rows, policy)
    assert result["status"] == "labelled"
    assert result["reason"] == "stop"
    assert result["net_r"] < 0


def test_partial_future_history_cannot_be_a_completed_label(candidate, policy):
    rows = future_bars(candidate)
    assert outcome(candidate, rows[:-1], policy)["status"] == "incomplete_outcome"
    assert outcome(candidate, rows[1:], policy)["status"] == "incomplete_outcome"


def test_resampling_does_not_fill_missing_or_partial_bars():
    rows = packet()[0]["15m"]
    hourly = resample(rows, 3600)
    assert len(hourly) == 75
    assert len(resample(rows[1:], 3600)) == 74
    assert sum(bar.volume for bar in hourly) == sum(bar.volume for bar in rows)


def test_confidence_interval_uses_time_blocks_and_insufficient_blocks_fail():
    rows = [{"entry_at": NOW + index * 86400, "net_r": 0.2} for index in range(100)]
    low, high = confidence_interval(rows, samples=200)
    assert low == pytest.approx(0.2)
    assert high == pytest.approx(0.2)
    low, high = confidence_interval(rows[:5], samples=200)
    assert low < 0 < high


def test_simple_trend_baseline_includes_costs_and_keeps_flat_market_in_cash(policy):
    rows = packet()[0]["15m"]
    result = trend_baseline(rows, NOW - 50 * 900, policy)
    free = policy.model_copy(update={"fee_per_side_bps": 0, "slippage_per_side_bps": 0, "cost_buffer_bps": 0})
    assert result["net_return"] < trend_baseline(rows, NOW - 50 * 900, free)["net_return"]
    assert result["side_transactions"] == 2
    assert result["costs_included"]
    flat = [bar.model_copy(update={"open": 60000.0, "close": 60000.0, "high": 60001.0, "low": 59999.0}) for bar in rows]
    assert trend_baseline(flat, NOW - 50 * 900, policy)["net_return"] == 0
