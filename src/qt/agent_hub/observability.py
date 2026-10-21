"""Optional, bounded OTLP traces; the local governance ledger remains mandatory.

No exporter, network, or SDK initialization occurs without an explicit endpoint.
Only identifiers, bounded labels, booleans and numeric usage counters are emitted.
No auto-instrumentation captures prompts, HTTP bodies, tool input or exceptions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from contextlib import suppress
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

_IDENTIFIER = re.compile(r"[A-Za-z0-9_:.@-]{1,128}")
_LABEL = re.compile(r"[A-Za-z0-9_.:/-]{1,100}")
_KINDS = {
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


def safe_metadata(metadata: dict | None) -> dict[str, str | int | bool]:
    result: dict[str, str | int | bool] = {}
    if not isinstance(metadata, dict):
        return result
    for key in ("model", "provider", "error_type", "validation_status", "reason"):
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
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

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
        headers={},
        timeout=2,
        session=session,
        max_request_size=262144,
    )
    return build_provider(exporter)


def export_step(
    tenant: str,
    run_id: str,
    step_id: str,
    kind: str,
    status: str,
    duration: float,
    metadata: dict | None = None,
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
