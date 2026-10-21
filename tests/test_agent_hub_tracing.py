"""Actual call traces, trace tree export, diagnostics, retry and privacy contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from qt.agent_hub.governance import Governance
from qt.agent_hub.observability import safe_fields
from qt.agent_hub.tracing import traceparent


def test_nested_calls_preserve_real_tree_and_attempt_scope(tmp_path: Path) -> None:
    ledger = Governance(tmp_path)
    assert traceparent() is None
    with ledger.invocation("owner", "run", "root", "pipeline", worker_id="worker-1") as root:
        assert traceparent() == f"00-{root['trace_id']}-{root['span_id']}-01"
    with ledger.invocation(
        "owner", "run", "job", "research", attempt=2, parent_step_id="root"
    ) as agent:
        ledger.call(
            "retrieval", "context.search", lambda secret: {"rows": [secret]}, "PRIVATE DOCUMENT"
        )
        with ledger.observe("tool", "research.collect") as tool:
            ledger.call("tool", "market.quote", lambda: {"price": 10})
        ledger.call(
            "model",
            "codex.invoke",
            lambda: {
                "answer": "PRIVATE ANSWER",
                "_runtime": {
                    "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 20}
                },
            },
            metadata={"model": "account-default", "prompt": "PRIVATE PROMPT"},
        )
    spans = ledger.spans("owner", "run")
    assert len(spans) == 6
    assert len({span["trace_id"] for span in spans}) == 1
    assert agent["parent_span_id"] == root["span_id"]
    assert (
        next(s for s in spans if s["name"] == "market.quote")["parent_span_id"] == tool["span_id"]
    )
    assert all(s["attempt"] == 2 for s in spans if s["step_id"] == "job")
    assert traceparent() is None
    serialized = json.dumps(spans)
    assert "PRIVATE" not in serialized and "prompt" not in serialized
    assert all(
        s["metadata"].get("input_sha256") for s in spans if s["operation"] in {"retrieval", "model"}
    )
    diagnostic = ledger.diagnostics("owner", "run")
    assert diagnostic["model_calls"] == 1 and diagnostic["tool_calls"] == 3
    assert diagnostic["retry_attempts"] == 0 and diagnostic["usage"]["input_tokens"] == 100
    assert diagnostic["max_lease_token"] == 2
    assert not diagnostic["incomplete"] and not diagnostic["truncated"]
    assert ledger.find_run_by_trace_id("owner", root["trace_id"]) == "run"
    assert ledger.find_run_by_trace_id("other", root["trace_id"]) is None
    assert not ledger.spans("other", trace_id=root["trace_id"])


def test_errors_record_safe_type_code_without_raw_message_and_survive_restart(
    tmp_path: Path,
) -> None:
    ledger = Governance(tmp_path)

    def failure() -> None:
        raise TimeoutError("Authorization: private-token")

    with (
        pytest.raises(TimeoutError, match="private-token"),
        ledger.invocation("owner", "run", "job", "research"),
    ):
        ledger.call("tool", "market.history", failure)
    diagnostic = Governance(tmp_path).diagnostics("owner", "run")
    assert diagnostic["failed_calls"] == 1 and diagnostic["failed_attempts"] == 1
    assert all(row["error_code"] == "operation_timeout" for row in diagnostic["errors"])
    assert "private-token" not in json.dumps(diagnostic)
    assert traceparent() is None


def test_started_record_is_visible_before_call_finishes_and_fencing_is_preserved(
    tmp_path: Path,
) -> None:
    ledger = Governance(tmp_path)
    with ledger.invocation("owner", "run", "job", "forecast") as span:
        assert Governance(tmp_path).diagnostics("owner", "run")["incomplete"]
        span["status"] = "fenced"
        span["metadata"]["accepted"] = False
    assert ledger.spans("owner")[0]["status"] == "fenced"
    assert ledger.spans("owner")[0]["metadata"]["accepted"] is False


def test_instrumentation_io_failure_never_retries_business_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Governance(tmp_path)
    calls = []

    def broken():
        raise sqlite3.OperationalError("private path")

    monkeypatch.setattr(ledger, "_db", broken)
    with ledger.invocation("owner", "run", "job", "execute"):
        assert ledger.call("tool", "trading.submit", lambda: calls.append(1)) is None
    assert calls == [1]


def test_unscoped_call_and_cross_ledger_call_do_not_mix_tenants(tmp_path: Path) -> None:
    first, second = Governance(tmp_path / "first"), Governance(tmp_path / "second")
    assert first.call("tool", "market.quote", lambda: 7) == 7
    with first.invocation("owner", "run", "job", "monitor"):
        assert second.call("tool", "market.quote", lambda: 8) == 8
    assert len(first.spans("owner")) == 1 and not second.spans("owner")


def test_sdk_export_matches_persisted_ids_usage_and_parent_without_phantom_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from qt.agent_hub import observability

    exporter = InMemorySpanExporter()
    provider = observability.build_provider(exporter, batch=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    monkeypatch.setattr(observability, "_provider", lambda _: provider)
    ledger = Governance(tmp_path)
    try:
        with ledger.invocation("owner", "run", "job", "research"):
            ledger.call(
                "model",
                "codex.invoke",
                lambda: {
                    "_runtime": {
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 50,
                            "output_tokens": 20,
                            "reasoning_output_tokens": 5,
                        }
                    }
                },
                metadata={"model": "synthetic", "provider": "local-codex"},
            )
        ledger.trace("owner", "run", "job", "research", "succeeded", 0.1)
        exported = exporter.get_finished_spans()
        local = ledger.spans("owner", "run")
        assert len(exported) == len(local) == 2
        assert {format(s.context.span_id, "016x") for s in exported} == {
            s["span_id"] for s in local
        }
        assert {format(s.context.trace_id, "032x") for s in exported} == {
            s["trace_id"] for s in local
        }
        model, agent = exported
        assert model.parent.span_id == agent.context.span_id
        assert agent.parent is None
        assert model.attributes["gen_ai.usage.input_tokens"] == 100
        assert json.loads(model.attributes["langfuse.observation.usage_details"]) == {
            "input": 100,
            "output": 20,
        }
        assert model.attributes["langfuse.observation.type"] == "generation"
        assert model.attributes["langfuse.session.id"] == agent.attributes["langfuse.session.id"]
    finally:
        provider.shutdown()


def test_langfuse_transport_auth_is_private_and_uses_v4_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qt.agent_hub.observability import _export_headers

    monkeypatch.setenv("QT_AGENT_HUB_LANGFUSE_PUBLIC_KEY", "pk-lf-fixture")
    monkeypatch.setenv("QT_AGENT_HUB_LANGFUSE_SECRET_KEY", "sk-lf-secret")
    headers = _export_headers()
    assert headers["Authorization"].startswith("Basic ")
    assert headers["x-langfuse-ingestion-version"] == "4"
    assert (
        safe_fields(
            {
                "Authorization": headers["Authorization"],
                "key": "sk-lf-secret",
                "error_type": "raw secret",
            }
        )
        == {}
    )
    monkeypatch.setenv("QT_AGENT_HUB_LANGFUSE_SECRET_KEY", "bad\nheader")
    with pytest.raises(ValueError, match="invalid_langfuse_credentials"):
        _export_headers()


def test_retry_diagnostics_count_recorded_attempts_and_separate_busy_deferrals(
    tmp_path: Path,
) -> None:
    ledger = Governance(tmp_path)

    class CodexBusyError(Exception):
        pass

    def busy() -> None:
        raise CodexBusyError("private provider details")

    with (
        pytest.raises(CodexBusyError),
        ledger.invocation("owner", "run", "one", "research", attempt=1),
    ):
        ledger.call("model", "codex.invoke", busy)
    with ledger.invocation("owner", "run", "one", "research", attempt=7):
        ledger.call("model", "codex.invoke", lambda: {})
    with ledger.invocation("owner", "run", "two", "analysis", attempt=99):
        ledger.call("model", "codex.invoke", lambda: {})
    result = ledger.diagnostics("owner", "run")
    assert result["retry_attempts"] == 1 and result["retried_steps"] == 1
    assert result["attempt_count"] == 3 and result["max_lease_token"] == 99
    assert result["deferred_calls"] == 1 and result["deferred_attempts"] == 1
    assert result["failed_calls"] == result["failed_attempts"] == 0
    assert 'qt_agent_model_tokens_total{direction="inputs"} 0' in ledger.metrics("owner")
