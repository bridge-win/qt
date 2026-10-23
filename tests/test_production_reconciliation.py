"""Read-only reconciliation and frozen-knowledge evidence regression tests."""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from qt.contracts import canonical_json
from qt.control.api import create_app
from qt.control.service import ControlService
from qt.control.worker import reconcile_paper_once, tool_broker
from qt.execution.simulated import PaperAdapter
from qt.persistence import Conflict
from qt.persistence.schema import artifacts, fills, orders
from qt.research.evidence import EvidenceDocument, EvidenceRepository
from qt.research.graph import ToolCall
from tests.test_execution_oms import book_adapter, intent, lease, reserve
from tests.test_execution_oms import state as state

_NOW = 1_700_000_000_000_000_000
_KEY = b"reconciliation-only-test-signing-key-32-bytes"


def signed_service(state, tmp_path, *, mode="paper", account_id="paper-account", identity="test-deployment"):
    service = ControlService(state.repo, tmp_path / "data", tenant="owner", signing_key=_KEY)
    manifest = state.deployment.model_copy(update={
        "mode": mode, "account_id": account_id, "deployment_id": identity,
        "risk_policy_hash": service.policy.fingerprint(),
    })
    unsigned = manifest.model_dump(mode="json", exclude={"signature"})
    signature = hmac.new(_KEY, canonical_json(unsigned).encode(), hashlib.sha256).hexdigest()
    state.deployment = manifest.model_copy(update={"signature": signature})
    service.deployments.activate("owner", state.deployment)
    return service


def client_for(service):
    return TestClient(create_app(service, token="reconcile-test"),
                      headers={"Authorization": "Bearer reconcile-test"})


def enqueue(service, key="reconcile"):
    response = client_for(service).post("/api/v4/reconciliation-runs",
                                       json={"account_id": "paper-account"},
                                       headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.asyncio
async def test_queued_reconciliation_recovers_paper_fills_without_any_resubmission(state, tmp_path, monkeypatch):
    service = signed_service(state, tmp_path)
    reserve(state, quantity="0.005", limit_price="10001", time_in_force="FOK", policy_hash=service.policy.fingerprint())
    adapter = book_adapter(state)
    await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    assert state.oms.get_order("owner", "one")["filled_quantity"] == 0
    job = enqueue(service)
    assert job["payload"]["deployment_id"] == state.deployment.deployment_id

    async def never(*args, **kwargs):
        raise AssertionError("reconciliation must never submit or cancel")
    monkeypatch.setattr(PaperAdapter, "submit", never)
    monkeypatch.setattr(PaperAdapter, "cancel", never)
    completed = await reconcile_paper_once(service)
    assert completed["status"] == "COMPLETED"
    assert completed["result"]["status"] == "reconciled"
    assert completed["result"]["resubmitted_orders"] == 0
    assert state.oms.get_order("owner", "one")["status"] == "FILLED"
    assert state.oms.balances("owner", "paper-account")["wallet"]["BTC"] == Decimal("0.005")
    enqueue(service, "replay")
    assert (await reconcile_paper_once(service))["status"] == "COMPLETED"
    assert len(state.oms.get_order("owner", "one")["fills"]) == 1
    assert await reconcile_paper_once(service) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "live_canary"])
async def test_nonpaper_accounts_are_rejected_at_queue_and_worker(state, tmp_path, mode):
    service = signed_service(state, tmp_path, mode=mode)
    response = client_for(service).post("/api/v4/reconciliation-runs", json={"account_id": "paper-account"},
                                        headers={"Idempotency-Key": "not-paper"})
    assert response.status_code == 403
    service.repository.enqueue("owner", "reconciliation", {"account_id": "paper-account"}, "internal-invalid")
    blocked = await reconcile_paper_once(service)
    assert blocked["status"] == "FAILED"
    assert blocked["result"]["status"] == "blocked"
    assert blocked["result"]["resubmitted_orders"] == 0


def test_reconciliation_api_rejects_unknown_or_uninitialized_accounts(state, tmp_path):
    service = signed_service(state, tmp_path, account_id="not-initialized")
    client = client_for(service)
    for account in ("not-initialized", "unknown"):
        assert client.post("/api/v4/reconciliation-runs", json={"account_id": account},
                           headers={"Idempotency-Key": account}).status_code == 404


@pytest.mark.asyncio
async def test_queued_old_deployment_cannot_select_a_new_account_head(state, tmp_path):
    service = signed_service(state, tmp_path)
    enqueue(service)
    signed_service(state, tmp_path, identity="new-version")
    result = await reconcile_paper_once(service)
    assert result["status"] == "FAILED" and result["result"]["status"] == "blocked"
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"


@pytest.mark.asyncio
async def test_reconciliation_consumes_real_pages_beyond_two_hundred_orders(state, tmp_path):
    service = signed_service(state, tmp_path)
    adapter = book_adapter(state, asks=[("10001", "1")])
    for number in range(201):
        identity = f"page-{number:03d}"
        reserve(state, identity, quantity="0.0001", limit_price="10001", time_in_force="FOK",
                policy_hash=service.policy.fingerprint())
        with state.repo.transaction() as connection:
            connection.execute(update(orders).where(orders.c.intent_id == identity).values(status="SUBMITTING"))
        await adapter.submit(intent(state, identity, quantity="0.0001", limit_price="10001", time_in_force="FOK",
                                    policy_hash=service.policy.fingerprint()))
        # Once paper ACK is durable, recoverable source state is no longer UNKNOWN.
        with state.repo.transaction() as connection:
            connection.execute(update(orders).where(orders.c.intent_id == identity).values(status="ACKNOWLEDGED"))
    enqueue(service)
    result = await reconcile_paper_once(service)
    assert result["status"] == "COMPLETED", result
    assert result["result"]["pages"] == 2
    with state.repo.transaction() as connection:
        assert len(connection.execute(select(fills)).all()) == 201
    assert state.oms.balances("owner", "paper-account")["wallet"]["BTC"] == Decimal("0.0201")


def document(text="known source", **extra):
    return EvidenceDocument(**{
        "title": "BTC evidence", "source": "fixture://source", "text": text,
        "source_hash": "a" * 64, "published_at_ns": _NOW - 100,
        "available_at_ns": _NOW - 90, "received_at_ns": _NOW - 80,
        "revision_id": "v1", "rights": "test-only", **extra,
    })


def test_ingesting_a_backdated_document_does_not_change_prior_knowledge(state):
    evidence = EvidenceRepository(state.repo)
    row = evidence.ingest("owner", document())
    with pytest.raises(KeyError, match="not_yet_observable"):
        evidence.read("owner", row["entity_id"], asof_ns=_NOW - 1)
    assert evidence.search("owner", "known", asof_ns=_NOW - 1)["items"] == []
    assert evidence.read("owner", row["entity_id"], asof_ns=_NOW)["text"] == "known source"


def test_long_evidence_is_read_in_bounded_hash_bound_excerpts(state):
    evidence = EvidenceRepository(state.repo)
    text = "可验证证据" * 5000
    row = evidence.ingest("owner", document(text))
    service = type("EvidenceService", (), {"repository": state.repo})()
    broker = tool_broker(service, asof_ns=_NOW)
    first = broker.invoke("owner", ToolCall(name="evidence.read", arguments={"identity": row["entity_id"], "length": 3000}))
    assert first["text"] == text[:3000]
    assert first["is_excerpt"] and first["next_offset"] == 3000
    assert first["recorded_at_ns"] == _NOW
    assert len(json.dumps(first).encode()) < 32000
    second = evidence.read("owner", row["entity_id"], asof_ns=_NOW, offset=3000, length=3000)
    assert second["text"] == text[3000:6000]
    assert second["excerpt_sha256"] != first["excerpt_sha256"]
    with pytest.raises(ValueError, match="bounded_evidence"):
        evidence.read("owner", row["entity_id"], asof_ns=_NOW, length=3001)


def test_artifact_read_and_list_obey_frozen_server_publication_time_and_integrity(state):
    service = type("EvidenceService", (), {"repository": state.repo})()
    late = state.repo.put("owner", "strategy", {"created_ns": _NOW - 1000, "available_at_ns": 0})
    broker = tool_broker(service, asof_ns=_NOW - 1)
    assert broker.invoke("owner", ToolCall(name="artifacts.list", arguments={"kind": "strategy"}))["items"] == []
    with pytest.raises(KeyError, match="not_yet_observable"):
        broker.invoke("owner", ToolCall(name="artifacts.read", arguments={"kind": "strategy", "identity": late["entity_id"]}))
    visible = tool_broker(service, asof_ns=_NOW)
    result = visible.invoke("owner", ToolCall(name="artifacts.read", arguments={"kind": "strategy", "identity": late["entity_id"]}))
    assert result["recorded_at_ns"] == _NOW
    with state.repo.transaction() as connection:
        connection.execute(update(artifacts).where(artifacts.c.entity_id == late["entity_id"]).values(payload={"tampered": True}))
    with pytest.raises(Conflict, match="integrity_failure"):
        visible.invoke("owner", ToolCall(name="artifacts.list", arguments={"kind": "strategy"}))


def test_large_artifact_tools_return_bounded_excerpt_not_an_unusable_full_blob(state):
    service = type("EvidenceService", (), {"repository": state.repo})()
    large = state.repo.put("owner", "experiment_result", {"rows": ["x" * 100] * 1000})
    broker = tool_broker(service, asof_ns=_NOW)
    result = broker.invoke("owner", ToolCall(name="artifacts.read", arguments={"kind": "experiment_result", "identity": large["entity_id"]}))
    assert result["is_excerpt"] is True and result["content"] is None
    assert result["next_offset"] == 2000
    assert len(json.dumps(result).encode()) < 32000
