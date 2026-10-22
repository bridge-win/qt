"""Actual SDK spans and exporter failure isolation; never a remote collector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qt.agent_hub import observability


def test_otlp_default_has_no_sdk_initialization_or_export(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(observability, "_provider", lambda _: pytest.fail("disabled exporter"))
    assert observability.export_step("personal", "run", "step", "research", "succeeded", 1) is False


def test_metadata_contains_only_safe_numeric_usage_and_labels() -> None:
    values = observability.safe_metadata(
        {
            "model": "gpt-model",
            "provider": "local-codex",
            "lease_token": 2,
            "accepted": True,
            "evidence_valid": False,
            "proposal_hash": "a" * 64,
            "usage": {
                "input_tokens": 123,
                "output_tokens": "secret",
                "cached_input_tokens": True,
                "reasoning_output_tokens": -1,
                "api_key": "hidden",
            },
            "prompt": "PRIVATE PROMPT",
            "answer": "PRIVATE ANSWER",
            "error_type": "RuntimeError: Authorization=secret",
            "reason": "secret has spaces",
        }
    )
    assert values["qt.usage.input_tokens"] == 123
    assert values["qt.accepted"] is True and values["qt.evidence_valid"] is False
    assert values["qt.lease_token"] == 2 and values["qt.proposal_hash"] == "a" * 64
    text = json.dumps(values)
    for private in ("secret", "hidden", "PRIVATE", "RuntimeError", "output_tokens"):
        assert private not in text


@pytest.mark.parametrize(
    "value",
    [
        "ftp://collector",
        "https://user:password@collector",
        "https://collector/?token=secret",
        "https://collector/#secret",
        "http://collector:99999",
        "http://bad host",
    ],
)
def test_endpoint_rejects_embedded_credentials_or_unsupported_protocol(value: str) -> None:
    with pytest.raises(ValueError, match="invalid_otlp_endpoint"):
        observability._endpoint(value)


def test_sdk_correlates_tenant_run_steps_attempts_and_error_without_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    exporter = InMemorySpanExporter()
    provider = observability.build_provider(exporter, batch=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setattr(observability, "_provider", lambda _: provider)
    try:
        for tenant, step, status in (
            ("alice", "one", "succeeded"),
            ("alice", "one", "failed"),
            ("alice", "two", "succeeded"),
            ("bob", "one", "succeeded"),
        ):
            assert observability.export_step(
                tenant,
                "run",
                step,
                "research",
                status,
                0.25,
                {
                    "usage": {"input_tokens": 100, "output_tokens": 20, "raw": "private"},
                    "prompt": "private",
                    "raw_exception": "Authorization: private",
                    "error_type": "TimeoutError",
                },
            )
        spans = exporter.get_finished_spans()
        assert len(spans) == 4 and len({row.context.span_id for row in spans}) == 4
        assert {row.context.trace_id for row in spans[:3]} == {
            int(observability.trace_id("alice", "run"), 16)
        }
        assert spans[3].context.trace_id != spans[0].context.trace_id
        assert spans[0].parent.span_id == spans[1].parent.span_id == spans[2].parent.span_id
        assert spans[1].status.status_code == StatusCode.ERROR
        assert spans[1].status.description is None
        assert spans[0].end_time - spans[0].start_time == 250_000_000
        assert spans[0].attributes["qt.step_id"] == "one"
        assert spans[2].attributes["qt.step_id"] == "two"
        assert spans[0].attributes["gen_ai.conversation.id"] == "alice:run"
        assert spans[0].attributes["gen_ai.operation.name"] == "invoke_agent"
        assert spans[0].attributes["qt.usage.output_tokens"] == 20
        assert "private" not in " ".join(row.to_json() for row in spans)
        assert all(not row.events for row in spans)
    finally:
        provider.shutdown()


def test_exporter_failure_does_not_log_raw_exception_or_change_local_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from qt.agent_hub.governance import Governance

    class BrokenExporter:
        def export(self, spans: object) -> None:
            raise RuntimeError("Authorization: fixture-secret")

        def shutdown(self) -> None:
            raise RuntimeError("fixture-secret")

    provider = observability.build_provider(BrokenExporter(), batch=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    monkeypatch.setattr(observability, "_provider", lambda _: provider)
    ledger = Governance(tmp_path)
    try:
        ledger.trace("alice", "run", "one", "research", "succeeded", 0.1)
        # Calling directly also proves safe failure even before the governance
        # call site is wired; the returned boolean means queued, not delivered.
        observability.export_step("alice", "run", "one", "research", "succeeded", 0.1)
        assert ledger.traces("alice", "run")[0]["status"] == "succeeded"
        assert "fixture-secret" not in caplog.text
    finally:
        provider.shutdown()
    assert "fixture-secret" not in caplog.text


def test_invalid_export_configuration_and_fields_are_best_effort(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")

    def failure(_: str) -> None:
        raise RuntimeError("private-token")

    monkeypatch.setattr(observability, "_provider", failure)
    assert not observability.export_step("alice", "run", "one", "research", "succeeded", 0.1)
    assert not observability.export_step("../alice", "run", "one", "research", "succeeded", 0.1)
    assert not observability.export_step(
        "alice", "run", "one", "research", "succeeded", float("nan")
    )
    assert "private-token" not in caplog.text


def test_otlp_factory_uses_explicit_bounded_transport_without_proxy_or_netrc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_exporter = pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    captured = {}

    def exporter(**kwargs):
        captured.update(kwargs)
        return InMemorySpanExporter()

    monkeypatch.setattr(http_exporter, "OTLPSpanExporter", exporter)
    observability._provider.cache_clear()
    provider = observability._provider("http://collector.invalid:4318/prefix/")
    try:
        assert captured["endpoint"] == "http://collector.invalid:4318/prefix/v1/traces"
        assert captured["headers"] == {} and captured["session"].trust_env is False
        assert captured["timeout"] == 2 and captured["max_request_size"] == 262144
    finally:
        provider.shutdown()
        captured["session"].close()
        observability._provider.cache_clear()


@pytest.mark.parametrize("failure", ["exception", "remote_reason"])
def test_actual_otlp_sdk_redacts_internal_failure_diagnostics(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    import requests

    requests_seen = []

    def request(self, **kwargs):
        requests_seen.append(kwargs["url"])
        if failure == "exception":
            raise RuntimeError("Authorization: fixture-private-token")
        response = requests.Response()
        response.status_code = 401
        response.reason = "Authorization: fixture-private-token"
        response._content = b"fixture-private-token"
        return response

    original_build = observability.build_provider
    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(
        observability, "build_provider", lambda exporter: original_build(exporter, batch=False)
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    observability._provider.cache_clear()
    provider = observability._provider("http://collector.invalid")
    try:
        assert observability.export_step("alice", "run", "one", "research", "succeeded", 0.1)
        assert requests_seen == ["http://collector.invalid/v1/traces"]
        assert "local trace retained" in caplog.text
        assert "fixture-private-token" not in caplog.text
        assert "Authorization" not in caplog.text
    finally:
        provider.shutdown()
        observability._provider.cache_clear()
