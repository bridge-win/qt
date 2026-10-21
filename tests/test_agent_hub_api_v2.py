"""Authenticated v2 control-plane contracts with the actual local domain stores."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qt.agent_hub.api import create_app
from qt.agent_hub.config import ROLES, HubConfig, Role
from qt.agent_hub.runtime import HubRuntime

HEADERS = {"Authorization": "Bearer offline-api-v2-test"}


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HubRuntime:
    monkeypatch.setenv("QT_AGENT_HUB_SERVICE_TOKEN", "offline-api-v2-test")
    monkeypatch.delenv("QT_AGENT_HUB_RESEARCH_URL", raising=False)
    config = HubConfig(
        tenant_id="alpha",
        roles={role: Role(instruction="Review supplied evidence") for role in ROLES},
    )
    return HubRuntime(config, tmp_path / "state", tmp_path / "files")


@pytest.fixture
def client(runtime: HubRuntime) -> TestClient:
    return TestClient(create_app(runtime.config, runtime.root, runtime.files, runtime=runtime))


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/capabilities", None),
        ("GET", "/api/skills", None),
        ("GET", "/api/traces", None),
        ("GET", "/api/release/readiness", None),
        ("GET", "/api/orders/unknown", None),
        ("POST", "/api/jobs/unknown/cancel", {}),
        ("POST", "/api/risk-mode", {"mode": "frozen"}),
    ],
)
def test_v2_endpoints_require_owner_authentication(
    client: TestClient, method: str, path: str, body: dict | None
) -> None:
    assert client.request(method, path, json=body).status_code == 401


def test_capabilities_report_real_boundaries_and_skill_contracts(client: TestClient) -> None:
    response = client.get("/api/capabilities", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["version"] == "2.1.0"
    assert "funded-live-account-acceptance" in result["unverified_or_unimplemented"]
    assert result["mcp"]["protocol"] == "2026-07-28"
    skills = client.get("/api/skills", headers=HEADERS).json()["items"]
    assert {item["role"] for item in skills} == set(ROLES)
    assert all(len(item["sha256"]) == 64 for item in skills)
    assert all("trade.execute" not in item["allowed_tools"] for item in skills)


def test_document_api_requires_availability_provenance_for_historical_replay(
    client: TestClient,
) -> None:
    body = {"source": "release:original", "content": "BTC historical release", "valid_from": 100}
    ordinary = client.post("/api/documents", headers=HEADERS, json=body).json()
    assert client.get("/api/search?q=BTC&as_of=200", headers=HEADERS).json()["items"] == []
    backfill = {**body, "available_at": 120, "published_at": 110}
    assert client.post("/api/documents", headers=HEADERS, json=backfill).status_code == 422
    backfill["provenance"] = "archive:release-log"
    document = client.post("/api/documents", headers=HEADERS, json=backfill).json()
    assert document["revision"] == ordinary["revision"] + 1
    assert (
        client.get("/api/search?q=BTC&as_of=200", headers=HEADERS).json()["items"][0]["id"]
        == document["id"]
    )
    assert (
        client.get("/api/search?q=BTC&as_of=200&known_at=200", headers=HEADERS).json()["items"]
        == []
    )
    assert (
        client.post(
            "/api/documents", headers=HEADERS, json={**body, "observed_at": 100}
        ).status_code
        == 422
    )


def test_strategy_selection_is_server_bounded(client: TestClient, runtime: HubRuntime) -> None:
    assert (
        client.post("/api/jobs", headers=HEADERS, json={"strategy": "arbitrary_python"}).status_code
        == 422
    )
    accepted = client.post("/api/jobs", headers=HEADERS, json={"strategy": "ma_trend"})
    assert accepted.status_code == 202
    assert (
        runtime.store.get_job("alpha", accepted.json()["id"])["payload"]["strategy"] == "ma_trend"
    )


def test_risk_modes_persist_with_legacy_kill_compatibility(
    client: TestClient, runtime: HubRuntime
) -> None:
    for mode in ("reduce_only", "frozen", "active"):
        result = client.post("/api/risk-mode", headers=HEADERS, json={"mode": mode})
        assert result.status_code == 200 and result.json()["risk_mode"] == mode
        assert runtime.trading.status("alpha")["risk_mode"] == mode
        assert runtime.trading.status("bravo")["risk_mode"] == "active"
    assert (
        client.post("/api/risk-mode", headers=HEADERS, json={"mode": "bypass"}).status_code == 422
    )
    assert (
        client.post(
            "/api/risk-mode", headers=HEADERS, json={"mode": "active", "tenant_id": "bravo"}
        ).status_code
        == 422
    )
    assert (
        client.post("/api/kill", headers=HEADERS, json={"enabled": True}).json()["risk_mode"]
        == "frozen"
    )


def test_cancel_is_tenant_scoped_and_fences_a_whole_workflow(
    client: TestClient, runtime: HubRuntime
) -> None:
    root = runtime.store.enqueue("alpha", "pipeline", {}, idempotency_key="root")
    child = runtime.store.enqueue(
        "alpha", "research", {"pipeline_id": root["id"]}, idempotency_key="child"
    )
    foreign = runtime.store.enqueue("bravo", "pipeline", {}, idempotency_key="foreign")
    assert (
        client.post(f"/api/jobs/{foreign['id']}/cancel", headers=HEADERS, json={}).status_code
        == 404
    )
    result = client.post(f"/api/jobs/{child['id']}/cancel", headers=HEADERS, json={})
    assert result.status_code == 200 and result.json()["cancelled_jobs"] == 2
    for item in (root, child):
        assert runtime.store.get_job("alpha", item["id"])["error"] == "cancelled_by_owner"
        assert (
            client.post(f"/api/jobs/{item['id']}/retry", headers=HEADERS, json={}).status_code
            == 422
        )
    assert runtime.store.get_job("bravo", foreign["id"])["status"] == "queued"


def test_trace_query_never_discloses_other_tenant_or_raw_context(
    client: TestClient, runtime: HubRuntime
) -> None:
    runtime.governance.trace(
        "alpha",
        "run-one",
        "step-one",
        "research",
        "succeeded",
        1.25,
        {"provider": "local-codex", "prompt": "private-prompt", "credential": "private-token"},
    )
    runtime.governance.trace("alpha", "run-two", "step-two", "analysis", "succeeded", 2.5)
    runtime.governance.trace("bravo", "run-one", "foreign-step", "research", "failed", 3.5)
    response = client.get("/api/traces?run_id=run-one&tenant=bravo", headers=HEADERS)
    assert response.status_code == 200
    assert [item["step_id"] for item in response.json()["items"]] == ["step-one"]
    assert "private-prompt" not in response.text and "private-token" not in response.text
    assert client.get("/api/traces?limit=1001", headers=HEADERS).status_code == 422
    assert client.get("/api/traces?limit=0", headers=HEADERS).status_code == 422
    metrics = client.get("/metrics", headers=HEADERS).text
    assert 'qt_agent_steps_total{kind="research",status="succeeded"} 1' in metrics
    assert 'status="failed"' not in metrics


def test_order_detail_reads_immutable_local_intent_without_cross_tenant_leak(
    client: TestClient, runtime: HubRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "qt.agent_hub.trading._get_json",
        lambda *args, **kwargs: {
            "symbol": "BTCUSDT",
            "bidPrice": "50000",
            "askPrice": "50010",
            "bidQty": "10",
            "askQty": "10",
        },
    )
    intent = {
        "idempotency_key": "order-v2",
        "venue": "binance",
        "instrument": "BTCUSDT",
        "side": "BUY",
        "quantity": "0.0002",
        "limit_price": "50010",
        "quote_timestamp": time.time(),
    }
    own = runtime.trading.submit("alpha", intent)
    foreign = runtime.trading.submit("bravo", intent)
    result = client.get(f"/api/orders/{own['order_id']}", headers=HEADERS)
    assert (
        result.status_code == 200
        and result.json()["execution_intent"]["idempotency_key"] == "order-v2"
    )
    assert client.get(f"/api/orders/{foreign['order_id']}", headers=HEADERS).status_code == 404


def test_release_readiness_is_local_and_does_not_claim_live_acceptance(client: TestClient) -> None:
    result = client.get("/api/release/readiness", headers=HEADERS)
    assert result.status_code == 200
    report = result.json()
    assert report["live_ready"] is False
    assert isinstance(report["checks"], list) and isinstance(report["paper_ready"], bool)


@pytest.mark.parametrize(
    "body,code",
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": None}, -32600),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "read_status", "arguments": []},
            },
            -32602,
        ),
        ({"jsonrpc": "2.0", "id": 1, "method": "unimplemented/legacy"}, -32601),
    ],
)
def test_legacy_mcp_fails_closed_for_malformed_or_unsupported_requests(
    client: TestClient, body: dict, code: int
) -> None:
    result = client.post("/mcp", headers=HEADERS, json=body)
    assert result.status_code == 200 and result.json()["error"]["code"] == code


def test_custom_host_port_preserves_internal_loopback_origin(client, monkeypatch):
    monkeypatch.setenv("QT_AGENT_HUB_PORT", "18878")
    # Authentication still applies to both allowed loopback entry points.
    for origin in ("http://127.0.0.1:8878", "http://127.0.0.1:18878"):
        response = client.get("/api/status", headers={"Origin": origin})
        assert response.status_code == 401
        assert client.get("/api/status", headers={**HEADERS, "Origin": origin}).status_code == 200
    assert (
        client.get("/api/status", headers={"Origin": "https://attacker.invalid"}).status_code == 403
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/opportunities",
        "/api/opportunities/scans/missing",
        "/api/runs/missing",
        "/api/runs/missing/diagnostics",
        "/api/spans?run_id=missing",
        "/api/trace/missing/diagnostics",
    ],
)
def test_diagnostics_and_discovery_require_authentication(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_span_query_requires_bounded_identifiers(client: TestClient) -> None:
    for query in ("", "?trace_id=invalid", "?run_id=x&limit=1001"):
        assert client.get("/api/spans" + query, headers=HEADERS).status_code == 422
    assert client.get("/api/runs/absent/diagnostics", headers=HEADERS).status_code == 404
