"""Adversarial contracts for run evidence, cancellation, release and recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qt.agent_hub.backup import create_backup, verify_restore
from qt.agent_hub.context import ContextStore
from qt.agent_hub.evaluation import evaluate
from qt.agent_hub.governance import Governance
from qt.agent_hub.store import HubStore


def test_receipt_rejects_cross_tenant_forged_sources_and_modified_payload(tmp_path: Path) -> None:
    ledger = Governance(tmp_path)
    receipt = ledger.receipt(
        "owner",
        "run",
        "step",
        {"references": [{"id": "market:123", "sha256": "abc", "source": "market"}]},
    )
    assert ledger.validate("owner", receipt, ["market:123"])["valid"]
    assert not ledger.validate("other", receipt, ["market:123"])["valid"]
    assert not ledger.validate("owner", receipt, ["invented"])["valid"]
    assert not ledger.validate("owner", {**receipt, "references": []}, ["market:123"])["valid"]
    assert not ledger.validate("owner", {**receipt, "run_id": "another-run"}, ["market:123"])[
        "valid"
    ]


def test_cancellation_fences_active_child_and_prevents_retry_or_followup(tmp_path: Path) -> None:
    ledger = HubStore(tmp_path / "hub.sqlite3")
    root = ledger.enqueue("owner", "pipeline", {}, idempotency_key="root")
    claimed = ledger.claim("owner", "worker")
    assert claimed
    assert ledger.complete_and_enqueue(
        "owner",
        root["id"],
        "worker",
        claimed["lease_token"],
        {},
        next_kind="research",
        next_payload={"pipeline_id": root["id"]},
        idempotency_key="child",
    )
    child = ledger.claim("owner", "worker")
    assert child
    with pytest.raises(KeyError):
        ledger.cancel_run("other", child["id"])
    assert ledger.cancel_run("owner", child["id"])["cancelled_jobs"] == 1
    assert not ledger.heartbeat("owner", child["id"], "worker", child["lease_token"])
    assert not ledger.complete_and_enqueue(
        "owner",
        child["id"],
        "worker",
        child["lease_token"],
        {},
        next_kind="analysis",
        next_payload={"pipeline_id": root["id"]},
        idempotency_key="later",
    )
    with pytest.raises(ValueError, match="cancelled"):
        ledger.requeue_deadletter("owner", child["id"])
    with pytest.raises(ValueError, match="cancelled"):
        ledger.enqueue("owner", "analysis", {"pipeline_id": root["id"]}, idempotency_key="bypass")
    assert ledger.claim("owner", "worker") is None


def test_backup_restore_checks_owned_files_and_omits_imports_and_credentials(
    tmp_path: Path,
) -> None:
    state, files = tmp_path / "state", tmp_path / "files"
    state.mkdir()
    HubStore(state / "hub.sqlite3").enqueue("owner", "research", {}, idempotency_key="one")
    context = ContextStore(state / "context.sqlite3", files)
    document = context.ingest("owner", "fixture:source", "evidence bytes")
    (state / "auth.json").write_text("must not enter backup")
    imports = files / "owner" / "imports"
    imports.mkdir()
    (imports / "private.txt").write_text("externally managed")
    destination = tmp_path / "backup"
    manifest = create_backup(state, files, destination)
    assert len(manifest["files"]) == 3
    assert f"files/owner/documents/{document['id']}.txt" in manifest["files"]
    assert verify_restore(destination)["passed"]
    (destination / "hub.sqlite3").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="integrity"):
        verify_restore(destination)


def test_frozen_safety_evaluation_uses_production_workflow_and_no_network() -> None:
    report = evaluate()
    assert report["passed"], json.dumps(report)
    assert len(report["cases"]) == 5
    assert all(item["orders"] == 0 for item in report["cases"] if item["id"] != "valid")


def test_trace_redaction_and_metrics(tmp_path: Path) -> None:
    ledger = Governance(tmp_path)
    ledger.trace(
        "owner",
        "run",
        "step",
        "forecast",
        "failed",
        2.5,
        {"error_type": "ValueError", "prompt": "private", "token": "secret"},
    )
    trace = ledger.traces("owner")[0]
    assert trace["metadata"] == {"error_type": "ValueError"}
    assert len(trace["trace_id"]) == 32
    assert not ledger.traces("other")
    assert 'kind="forecast",status="failed"' in ledger.metrics("owner")
    assert "private" not in json.dumps(trace)
