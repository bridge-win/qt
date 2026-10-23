"""API -> queue -> real native worker -> report, plus bounded graph execution."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langgraph", reason="requires the locked Python 3.12 production extra")

from langgraph.checkpoint.memory import InMemorySaver

from qt.contracts import CallBudget, ModelRequest, ModelResult, content_hash
from qt.control.api import create_app
from qt.control.service import ControlService
from qt.control.worker import run_once
from qt.persistence import Repository, create_engine, migrate
from qt.persistence.legacy import import_sqlite_archive
from qt.research.evidence import EvidenceDocument, EvidenceRepository
from qt.research.graph import ResearchGraph, ToolBroker
from qt.research.providers import LocalModelProvider
from tests.test_experiment_native import native_input


@pytest.fixture
def control(tmp_path):
    engine = create_engine("sqlite:///:memory:", allow_sqlite=True)
    migrate(engine)
    service = ControlService(Repository(engine), tmp_path / "data", tenant="offline",
                             signing_key=b"test-only-signing-key-with-at-least-32-bytes")
    yield service
    engine.dispose()


def client_for(service):
    return TestClient(create_app(service, token="offline-secret"),
                      headers={"Authorization": "Bearer offline-secret"})


def test_authenticated_v4_api_and_empty_readiness(control):
    client = client_for(control)
    assert client.get("/healthz").json()["status"] == "process_running"
    assert TestClient(create_app(control, token="offline-secret")).get("/api/v4/readiness").status_code == 401
    response = client.get("/api/v4/readiness")
    assert response.status_code == 200 and response.json()["live_ready"] is False
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["browser_acceptance"] == "deferred_by_user"
    assert client.get("/api/v4/readiness", headers={"Origin": "https://untrusted.invalid"}).status_code == 403
    assert client.get("/api/v4/forecasts/scorecards").status_code == 200


def test_api_bounds_chunked_body_before_parsing_and_authenticates_first(control):
    client = client_for(control)
    oversized = (b"x" * 200_000 for _ in range(6))
    assert client.post("/api/v4/research-runs", content=oversized,
                       headers={"Content-Type": "application/json"}).status_code == 413
    unauthenticated = TestClient(create_app(control, token="offline-secret"))
    assert unauthenticated.post("/api/v4/research-runs", content=b"x" * 1_000_001).status_code == 401


def test_worker_claims_only_its_server_tenant(control):
    repo = control.repository
    repo.enqueue("another-tenant", "experiment", {"experiment_hash": "foreign"}, "foreign")
    assert run_once(control, "experiment") is None
    assert repo.claim("experiment", tenant="another-tenant")["payload"]["experiment_hash"] == "foreign"


def test_http_to_real_native_experiment_and_signed_paper_deployment(control, tmp_path):
    pytest.importorskip("nautilus_trader", reason="requires the pinned native production image")
    spec, strategy, registry, instruments, fees = native_input(tmp_path)
    control.datasets = registry
    client = client_for(control)
    for endpoint, contracts in (("strategy-versions", [strategy]), ("instruments", instruments),
                                ("fee-schedules", fees), ("datasets", [registry.load(spec.dataset_ids[0])])):
        for contract in contracts:
            response = client.post("/api/v4/" + endpoint, json=contract.model_dump(mode="json"))
            assert response.status_code == 201, response.text
    end = spec.split.test_end_ns + spec.timeframes_ns[0]
    campaign = client.post("/api/v4/campaigns", json={
        "campaign_id": spec.campaign_id, "dataset_id": spec.dataset_ids[0],
        "holdout_start_ns": end, "holdout_end_ns": end + 10 * spec.timeframes_ns[0],
        "protocol": {"primary_metric": "net_expectancy", "minimum_effect": "0.001"},
    })
    assert campaign.status_code == 201, campaign.text
    body = spec.model_dump(mode="json")
    body["instrument_spec_hashes"] = [item.content_hash() for item in instruments]
    body["policy_version"] = control.policy.version
    experiment = client.post("/api/v4/experiments", json=body, headers={"Idempotency-Key": "e2e"})
    assert experiment.status_code == 202, experiment.text
    assert client.post("/api/v4/experiments", json=body, headers={"Idempotency-Key": "e2e"}).json() == experiment.json()
    changed = {**body, "seed": 9}
    assert client.post("/api/v4/experiments", json=changed, headers={"Idempotency-Key": "e2e"}).status_code == 409
    completed = run_once(control, "experiment")
    assert completed["status"] == "COMPLETED", completed
    identity = experiment.json()["payload"]["experiment_hash"]
    result = client.get("/api/v4/experiments/" + identity).json()["payload"]
    assert result["folds"][0]["raw"]["result"]["account_invariants"]["status"] == "verified"
    assert result["gate_evidence"]["causality_verified"] is True
    report = client.post("/api/v4/validations", json={"experiment_hash": identity, "stage": "paper"})
    assert report.status_code == 201 and report.json()["payload"]["eligible"], report.text
    live_report = client.post("/api/v4/validations", json={"experiment_hash": identity, "stage": "live_canary"})
    assert not live_report.json()["payload"]["eligible"]
    now = time.time_ns()
    manifest = {"strategy_hash": strategy.content_hash(), "provider": "frozen_rules",
                "model_id": "deterministic-baseline-v1", "prompt_hash": "a" * 64, "tools_hash": "b" * 64,
                "risk_policy_hash": control.policy.fingerprint(),
                "fee_schedule_hashes": [fee.content_hash() for fee in fees],
                "validation_report_hash": report.json()["entity_id"], "venue": "binance", "account_id": "e2e-paper",
                "mode": "paper", "risk_budget": strategy.risk_budget.model_dump(mode="json"),
                "valid_from_ns": now - 1, "valid_until_ns": now + 600_000_000_000}
    deployment = client.post("/api/v4/deployments", json=manifest, headers={"Idempotency-Key": "e2e-paper"})
    assert deployment.status_code == 201, deployment.text
    assert control.verify_deployment(deployment.json()["entity_id"]).mode == "paper"
    assert client.get("/api/v4/readiness").json()["live_ready"] is False


class BoundedFixture:
    enforces_token_limits = True

    def __init__(self):
        self.calls = []

    async def invoke(self, request, budget):
        body = json.loads(request.prompt)
        self.calls.append(body)
        role = body["role"]
        if role == "research" and not body["evidence"].get("observations"):
            output = {"summary": "collect evidence", "decision": "hold", "evidence": [],
                      "tool_requests": [{"name": "evidence.read", "arguments": {"identity": "frozen"}}]}
        else:
            observations = body["evidence"].get("observations", [])
            output = {"summary": "testable candidate", "decision": "propose",
                      "evidence": [row["evidence_id"] for row in observations]}
        return ModelResult(provider="frozen_fixture", model_id=request.model_id, output=output,
                           input_tokens=100, output_tokens=50, cost_usd="0.01", raw_artifact_hash="a" * 64)


def test_same_graph_checkpoint_tools_skeptic_and_spend(control):
    provider = BoundedFixture()
    broker = ToolBroker({"evidence.read": lambda tenant, identity: {"id": identity, "tenant": tenant, "fact": "fixture"}})
    control.repository.enqueue(control.tenant, "research", {"question": "frozen question", "model_id": "frozen"}, "graph")
    job = control.repository.claim("research")
    graph = ResearchGraph(control.repository, provider, broker, InMemorySaver())
    result = asyncio.run(graph.run(job, model_id="frozen"))
    assert result["rounds"] == 2 and result["tool_calls"] == 1
    assert result["evidence_membership_verified"] is True
    assert result["factual_entailment_verified"] is False and result["execution_authorized"] is False
    assert [row["role"] for row in provider.calls] == ["research", "research", "skeptic"]
    assert "scratchpad" not in provider.calls[-1]["evidence"]


def test_fake_citation_is_held(control):
    class Fabricated(BoundedFixture):
        async def invoke(self, request, budget):
            return ModelResult(provider="fixture", model_id=request.model_id,
                               output={"decision": "propose", "evidence": ["invented-id"]},
                               input_tokens=1, output_tokens=1, cost_usd="0", raw_artifact_hash="a" * 64)
    repo = control.repository
    repo.enqueue(control.tenant, "research", {"question": "frozen"}, "fake")
    graph = ResearchGraph(repo, Fabricated(), ToolBroker({}), InMemorySaver())
    result = asyncio.run(graph.run(repo.claim("research"), model_id="frozen"))
    assert result["answer"]["decision"] == "hold"


def test_research_http_idempotency_cancellation_and_sse_cursor(control, monkeypatch):
    monkeypatch.setenv("QT_V4_MODEL_ID", "frozen")
    client = client_for(control)
    first = client.post("/api/v4/research-runs", json={"question": "frozen"}, headers={"Idempotency-Key": "a"})
    assert first.status_code == 202
    identity = first.json()["job_id"]
    assert client.post("/api/v4/research-runs", json={"question": "other"}, headers={"Idempotency-Key": "a"}).status_code == 409
    control.repository.claim("research")
    page = client.get(f"/api/v4/research-runs/{identity}/events").text
    assert "event: claimed" in page
    cursor = max(int(line[4:]) for line in page.splitlines() if line.startswith("id: "))
    assert "event: claimed" not in client.get(f"/api/v4/research-runs/{identity}/events", headers={"Last-Event-ID": str(cursor)}).text
    assert client.post(f"/api/v4/research-runs/{identity}/cancel").json()["cancel_requested"] == 1


def test_local_provider_preflights_actual_tokens_and_requires_complete_json():
    seen = []
    def serve(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        data = {"/apply-template": {"prompt": "frozen rendered"}, "/tokenize": {"tokens": [1, 2, 3]},
                "/v1/chat/completions": {"model": "frozen", "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                                         "choices": [{"finish_reason": "stop", "message": {"content": '{"decision":"hold"}'}}]}}
        return httpx.Response(200, json=data[request.url.path])
    provider = LocalModelProvider("http://localhost:8080", model_id="frozen", transport=httpx.MockTransport(serve))
    request = ModelRequest(run_id="run", model_id="frozen", prompt="frozen", prompt_hash=content_hash("frozen"), tools_hash="a" * 64)
    budget = CallBudget(maximum_cost_usd="0", maximum_input_tokens=3, maximum_output_tokens=10, deadline_ns=time.time_ns() + 1_000_000_000)
    assert asyncio.run(provider.invoke(request, budget)).cost_usd == Decimal(0)
    assert seen[-1][1]["max_tokens"] == 10
    assert seen[-1][1]["response_format"] == {"type": "json_object"}
    with pytest.raises(ValueError, match="input_token_budget"):
        asyncio.run(provider.invoke(request, budget.model_copy(update={"maximum_input_tokens": 2})))


def test_versioned_evidence_and_legacy_archive_never_invent_canonical_results(control, tmp_path):
    now = time.time_ns()
    evidence = EvidenceRepository(control.repository)
    doc = EvidenceDocument(title="frozen BTC source", source="fixture://source", text="BTC evidence",
                           source_hash="a" * 64, published_at_ns=now - 10,
                           available_at_ns=now - 5, received_at_ns=now, revision_id="v1", rights="test")
    saved = evidence.ingest(control.tenant, doc)
    with pytest.raises(KeyError):
        evidence.read(control.tenant, saved["entity_id"], asof_ns=now - 1)
    assert evidence.search(control.tenant, "BTC", asof_ns=time.time_ns())["items"][0]["id"] == saved["entity_id"]
    old = tmp_path / "old.sqlite3"
    with sqlite3.connect(old) as db:
        db.execute("CREATE TABLE trials (id TEXT, value TEXT)")
        db.execute("INSERT INTO trials VALUES ('a','original')")
        db.execute("CREATE TABLE credentials (secret TEXT)")
        db.execute("INSERT INTO credentials VALUES ('must-not-import')")
    first = import_sqlite_archive(control.repository, control.tenant, old)
    assert import_sqlite_archive(control.repository, control.tenant, old) == first
    assert first["tables"][0]["rows"] == 1 and not first["trading_state_migrated"]
    assert {item["table"] for item in first["tables"]} == {"trials"}
    assert control.repository.list(control.tenant, "experiment_result") == []


def test_migration_is_idempotent_and_history_is_not_downgraded(control):
    migrate(control.repository.engine)
    assert client_for(control).get("/api/v4/readiness").status_code == 200
