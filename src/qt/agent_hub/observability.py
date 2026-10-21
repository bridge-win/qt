"""Optional, bounded OTLP traces; the local governance ledger remains mandatory.

No exporter, network, or SDK initialization occurs without an explicit endpoint.
Only identifiers, bounded labels, booleans and numeric usage counters are emitted.
No auto-instrumentation captures prompts, HTTP bodies, tool input or exceptions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import time
from contextlib import suppress
from contextvars import ContextVar
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

_IDENTIFIER = re.compile(r"[A-Za-z0-9_:.@-]{1,128}")
_LABEL = re.compile(r"[A-Za-z0-9_.:/-]{1,100}")
_KINDS = {
    "discover",
    "pipeline",
    "research",
    "analysis",
    "backtest",
    "forecast",
    "risk_review",
    "execute",
    "monitor",
    "import_context",
    "log_review",
}
_STATUSES = {"succeeded", "failed", "fenced", "deferred", "blocked", "held"}
_USAGE = {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}
_EXPLICIT_SPAN_ID: ContextVar[int | None] = ContextVar("qt_export_span_id", default=None)
_EXPLICIT_TRACE_ID: ContextVar[int | None] = ContextVar("qt_export_trace_id", default=None)


class _ExportDiagnosticFilter(logging.Filter):
    """The HTTP SDK handles some errors itself and logs their raw messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = "Agent Hub OTLP export diagnostic; local trace retained"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


_EXPORT_DIAGNOSTICS = _ExportDiagnosticFilter()


def trace_id(tenant: str, run_id: str) -> str:
    """Match Governance.traces without importing that ledger or its dependencies."""
    raw = json.dumps([tenant, run_id], sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def safe_metadata(metadata: dict[str, Any] | None) -> dict[str, str | int | bool]:
    result: dict[str, str | int | bool] = {}
    if not isinstance(metadata, dict):
        return result
    for key in (
        "model",
        "provider",
        "error_type",
        "error_code",
        "validation_status",
        "reason",
        "worker_id",
    ):
        value = metadata.get(key)
        if isinstance(value, str) and _LABEL.fullmatch(value):
            result[f"qt.{key}"] = value
    for key in ("accepted", "evidence_valid"):
        if type(metadata.get(key)) is bool:
            result[f"qt.{key}"] = metadata[key]
    lease = metadata.get("lease_token")
    if type(lease) is int and 0 <= lease <= 10**12:
        result["qt.lease_token"] = lease
    proposal = metadata.get("proposal_hash")
    if isinstance(proposal, str) and re.fullmatch(r"[0-9a-f]{64}", proposal):
        result["qt.proposal_hash"] = proposal
    usage = metadata.get("usage")
    if isinstance(usage, dict):
        for key in _USAGE:
            value = usage.get(key)
            if type(value) is int and 0 <= value <= 10**12:
                result[f"qt.usage.{key}"] = value
    return result


def safe_fields(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """The local and exported ledgers share the same redaction contract."""
    result: dict[str, Any] = {}
    for key, value in safe_metadata(metadata).items():
        key = key.removeprefix("qt.")
        if key.startswith("usage."):
            result.setdefault("usage", {})[key.removeprefix("usage.")] = value
        else:
            result[key] = value
    for key in ("input_sha256", "output_sha256", "release_sha256", "skill_sha256"):
        candidate = (metadata or {}).get(key)
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
            result[key] = candidate
    for key in (
        "input_bytes",
        "output_bytes",
        "input_items",
        "output_items",
        "attempt",
        "http_status",
    ):
        candidate = (metadata or {}).get(key)
        if type(candidate) is int and 0 <= candidate <= 10**12:
            result[key] = candidate
    return result


def build_provider(exporter: Any, *, batch: bool = True) -> Any:
    """Own the provider; do not mutate global SDK state or detect env resources."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        SimpleSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    class LedgerIds(RandomIdGenerator):
        def generate_span_id(self) -> int:
            return _EXPLICIT_SPAN_ID.get() or super().generate_span_id()

        def generate_trace_id(self) -> int:
            return _EXPLICIT_TRACE_ID.get() or super().generate_trace_id()

    class IsolatedExporter(SpanExporter):
        # The SDK normally logs raw exporter exceptions. Keep them out of
        # application logs and never let telemetry become a business failure.
        def export(self, spans: Any) -> Any:
            try:
                return exporter.export(spans)
            except Exception:
                return SpanExportResult.FAILURE

        def shutdown(self) -> None:
            with suppress(Exception):
                exporter.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            try:
                return bool(exporter.force_flush(timeout_millis))
            except Exception:
                return False

    provider = TracerProvider(
        resource=Resource({"service.name": "qt-agent-hub", "service.namespace": "qt"}),
        sampler=ALWAYS_ON,
        id_generator=LedgerIds(),
    )
    isolated = IsolatedExporter()
    processor = (
        BatchSpanProcessor(
            isolated,
            max_queue_size=256,
            max_export_batch_size=32,
            schedule_delay_millis=1000,
            export_timeout_millis=2000,
        )
        if batch
        else SimpleSpanProcessor(isolated)
    )
    provider.add_span_processor(processor)
    return provider


def _endpoint(base: str) -> str:
    parsed = urlsplit(base)
    if (
        len(base) > 2048
        or any(character.isspace() for character in base)
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid_otlp_endpoint")
    # Validate malformed/out-of-range ports without exposing the original URL.
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("invalid_otlp_endpoint") from None
    return base.rstrip("/") + "/v1/traces"


@lru_cache(maxsize=1)
def _provider(endpoint: str) -> Any:
    import requests
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    # The SDK may log remote HTTP reasons or request exceptions before returning
    # FAILURE. Filter just this exporter, preserving severity and local traces.
    logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter").addFilter(
        _EXPORT_DIAGNOSTICS
    )
    # Explicit HTTP/protobuf transport; no automatic global instrumentation or
    # default localhost exporter. Endpoint headers stay outside trace attributes.
    session = requests.Session()
    session.trust_env = False  # No proxy, netrc, or credential-provider inheritance.
    exporter = OTLPSpanExporter(
        endpoint=_endpoint(endpoint),
        headers=_export_headers(),
        timeout=2,
        session=session,
        max_request_size=262144,
    )
    return build_provider(exporter)


def _export_headers() -> dict[str, str]:
    """Credentials only enter transport headers; never span data or logs."""
    public = os.environ.get("QT_AGENT_HUB_LANGFUSE_PUBLIC_KEY", "")
    secret = os.environ.get("QT_AGENT_HUB_LANGFUSE_SECRET_KEY", "")
    if not public and not secret:
        return {}
    if not public or not secret or any(c in public + secret for c in "\r\n:"):
        raise ValueError("invalid_langfuse_credentials")
    if len(public) + len(secret) > 2048:
        raise ValueError("invalid_langfuse_credentials")
    encoded = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {encoded}", "x-langfuse-ingestion-version": "4"}


def export_span(record: dict[str, Any]) -> bool:
    """Export a persisted invocation using the exact local trace/span identities."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        return False
    token = trace_token = None
    try:
        from opentelemetry.context import Context
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            SpanKind,
            Status,
            StatusCode,
            TraceFlags,
            set_span_in_context,
        )

        operation = record["operation"]
        semantic = {
            "agent": "invoke_agent",
            "model": "chat",
            "tool": "execute_tool",
            "retrieval": "retrieval",
        }[operation]
        observation = {
            "agent": "agent",
            "model": "generation",
            "tool": "tool",
            "retrieval": "retriever",
        }[operation]
        parent_id = record.get("parent_span_id")
        identity = int(record["trace_id"], 16)
        context = Context()
        if parent_id:
            parent = SpanContext(
                identity, int(parent_id, 16), False, TraceFlags(TraceFlags.SAMPLED)
            )
            context = set_span_in_context(NonRecordingSpan(parent))
        fields = safe_fields(record.get("metadata"))
        attributes: dict[str, Any] = {
            "qt.tenant_id": record["tenant"],
            "qt.run_id": record["run_id"],
            "qt.step_id": record["step_id"],
            "qt.attempt": record["attempt"],
            "qt.status": record["status"],
            "gen_ai.operation.name": semantic,
            "langfuse.observation.type": observation,
            "langfuse.session.id": f"{record['tenant']}:{record['run_id']}",
            "langfuse.user.id": record["tenant"],
            "langfuse.trace.name": "qt-investment-workflow",
        }
        for key, value in fields.items():
            if key != "usage":
                attributes[f"qt.{key}"] = value
        if fields.get("model"):
            attributes["gen_ai.request.model"] = fields["model"]
            attributes["langfuse.observation.model.name"] = fields["model"]
        if fields.get("provider"):
            attributes["gen_ai.provider.name"] = fields["provider"]
        if operation == "agent":
            attributes["gen_ai.agent.name"] = record["name"]
        elif operation == "tool":
            attributes["gen_ai.tool.name"] = record["name"]
        usage = fields.get("usage", {})
        for key, value in usage.items():
            attributes[f"qt.usage.{key}"] = value
        for key in ("input_tokens", "output_tokens"):
            if key in usage:
                attributes[f"gen_ai.usage.{key}"] = usage[key]
        if usage:
            # Codex total input/output already include cached/reasoning tokens.
            # Keep overlapping counters in qt.usage only; Langfuse sums details.
            attributes["langfuse.observation.usage_details"] = json.dumps(
                {
                    key.removesuffix("_tokens"): usage[key]
                    for key in ("input_tokens", "output_tokens")
                    if key in usage
                }
            )
        token = _EXPLICIT_SPAN_ID.set(int(record["span_id"], 16))
        trace_token = _EXPLICIT_TRACE_ID.set(identity)
        span = (
            _provider(endpoint)
            .get_tracer("qt.agent_hub", "2")
            .start_span(
                f"{semantic} {record['name']}",
                context=context,
                kind=SpanKind.CLIENT if operation == "model" else SpanKind.INTERNAL,
                start_time=record["started_ns"],
                attributes=attributes,
            )
        )
        span.set_status(Status(StatusCode.ERROR if record["status"] == "failed" else StatusCode.OK))
        span.end(end_time=record["ended_ns"])
        return True
    except Exception:
        return False
    finally:
        if token is not None:
            _EXPLICIT_SPAN_ID.reset(token)
        if trace_token is not None:
            _EXPLICIT_TRACE_ID.reset(trace_token)


def flush(timeout_ms: int = 5000) -> bool:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        return False
    try:
        return bool(_provider(endpoint).force_flush(timeout_millis=timeout_ms))
    except Exception:
        return False


def export_step(
    tenant: str,
    run_id: str,
    step_id: str,
    kind: str,
    status: str,
    duration: float,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Queue a best-effort trace after local ledger commit; false means disabled/failed."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        return False
    try:
        if any(
            not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
            for value in (tenant, run_id, step_id)
        ):
            return False
        if isinstance(duration, bool) or not math.isfinite(duration) or not 0 <= duration <= 86400:
            return False
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            Status,
            StatusCode,
            TraceFlags,
            set_span_in_context,
        )

        provider = _provider(endpoint)
        identity = trace_id(tenant, run_id)
        parent = SpanContext(
            trace_id=int(identity, 16),
            span_id=int(hashlib.sha256(f"run:{identity}".encode()).hexdigest()[:16], 16) or 1,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        kind = kind if kind in _KINDS else "other"
        status = status if status in _STATUSES else "unknown"
        end = time.time_ns()
        span = provider.get_tracer("qt.agent_hub", "1").start_span(
            f"agent.{kind}",
            context=set_span_in_context(NonRecordingSpan(parent)),
            start_time=end - int(duration * 1_000_000_000),
            attributes={
                "qt.tenant_id": tenant,
                "qt.run_id": run_id,
                "qt.step_id": step_id,
                "qt.kind": kind,
                "qt.status": status,
                **safe_metadata(metadata),
            },
        )
        span.set_status(Status(StatusCode.ERROR if status == "failed" else StatusCode.OK))
        span.end(end_time=end)
        return True
    except Exception:
        # Never format/log exporter exceptions, endpoints, headers or payloads.
        return False
