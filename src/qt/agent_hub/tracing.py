"""Persistent, nested invocation tracing with content-free call fingerprints.

The workflow database owns task state; these records diagnose calls, including
process interruption (a retained running span), without authorizing side effects.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypeVar

from qt.agent_hub.observability import export_span, safe_fields, trace_id

_CURRENT: ContextVar[dict[str, Any] | None] = ContextVar("qt_invocation", default=None)
_NAME = re.compile(r"[A-Za-z0-9_.:/-]{1,100}")
T = TypeVar("T")


def traceparent() -> str | None:
    """W3C context for trusted executor requests; never propagate baggage."""
    current = _CURRENT.get()
    return f"00-{current['trace_id']}-{current['span_id']}-01" if current else None


def _fingerprint(value: Any, prefix: str) -> dict[str, Any]:
    def bounded(item: Any, depth: int = 0) -> Any:
        if depth > 8:
            return {"type": type(item).__name__, "truncated": True}
        if isinstance(item, str):
            return item[:262144]
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else "nonfinite"
        if isinstance(item, dict):
            return {str(k): bounded(v, depth + 1) for k, v in list(item.items())[:1000]}
        if isinstance(item, (list, tuple)):
            return [bounded(v, depth + 1) for v in item[:1000]]
        return {"type": type(item).__name__}

    raw = json.dumps(bounded(value), sort_keys=True, ensure_ascii=False).encode()
    return {
        f"{prefix}_sha256": hashlib.sha256(raw).hexdigest(),
        f"{prefix}_bytes": len(raw),
        f"{prefix}_items": len(value) if isinstance(value, (list, tuple, dict)) else 1,
    }


class InvocationLedger:
    path: Path

    def _db(self) -> Any:
        raise NotImplementedError

    def initialize_spans(self, db: sqlite3.Connection) -> None:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS invocation_spans (
                span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL,
                tenant TEXT NOT NULL, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, operation TEXT NOT NULL, name TEXT NOT NULL,
                parent_span_id TEXT, status TEXT NOT NULL,
                started_ns INTEGER NOT NULL, ended_ns INTEGER,
                duration REAL, metadata TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS invocation_run
                ON invocation_spans(tenant,run_id,started_ns);
            CREATE INDEX IF NOT EXISTS invocation_trace
                ON invocation_spans(tenant,trace_id,started_ns);
        """)

    @contextmanager
    def invocation(
        self,
        tenant: str,
        run_id: str,
        step_id: str,
        role: str,
        *,
        attempt: int = 1,
        worker_id: str = "worker",
        parent_step_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        parent_span_id = None
        if parent_step_id:
            with self._db() as db:
                parent = db.execute(
                    "SELECT span_id FROM invocation_spans WHERE tenant=? AND run_id=? "
                    "AND step_id=? AND operation='agent' ORDER BY started_ns DESC LIMIT 1",
                    (tenant, run_id, parent_step_id),
                ).fetchone()
            parent_span_id = parent[0] if parent else None
        scope = {
            "tenant": tenant,
            "run_id": run_id,
            "step_id": step_id,
            "trace_id": trace_id(tenant, run_id),
            "attempt": attempt,
            "span_id": parent_span_id,
            "ledger_path": str(self.path),
        }
        token = _CURRENT.set(scope)
        try:
            with self.observe("agent", role, metadata={"worker_id": worker_id}) as span:
                yield span
        finally:
            _CURRENT.reset(token)

    @contextmanager
    def observe(
        self,
        operation: str,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        current = _CURRENT.get()
        if not current or current["ledger_path"] != str(self.path):
            # Direct unit/domain calls outside a worker are intentionally untraced.
            yield {}
            return
        operation = operation if operation in {"agent", "model", "tool", "retrieval"} else "tool"
        name = name if _NAME.fullmatch(name) else "other"
        record = {
            **{k: current[k] for k in ("trace_id", "tenant", "run_id", "step_id", "attempt")},
            "span_id": uuid.uuid4().hex[:16],
            "parent_span_id": current["span_id"],
            "operation": operation,
            "name": name,
            "status": "running",
            "started_ns": time.time_ns(),
            "ended_ns": None,
            "duration": None,
            "metadata": safe_fields(metadata),
        }
        self._save_span(record)
        token = _CURRENT.set({**current, "span_id": record["span_id"]})
        started = time.monotonic()
        try:
            yield record
            if record["status"] == "running":
                record["status"] = "succeeded"
        except BaseException as error:
            record["status"] = "deferred" if type(error).__name__ == "CodexBusyError" else "failed"
            record["metadata"]["error_type"] = type(error).__name__
            record["metadata"]["error_code"] = {
                "ConnectError": "upstream_connection",
                "HTTPStatusError": "upstream_http_status",
                "ReadTimeout": "upstream_timeout",
                "TimeoutError": "operation_timeout",
                "CodexBusyError": "executor_busy",
                "ValueError": "input_or_contract_invalid",
                "PermissionError": "permission_denied",
            }.get(type(error).__name__, "operation_failed")
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            if type(status_code) is int and 100 <= status_code <= 599:
                record["metadata"]["http_status"] = status_code
            raise
        finally:
            record["ended_ns"] = time.time_ns()
            record["duration"] = max(0, time.monotonic() - started)
            record["metadata"] = safe_fields(record["metadata"])
            _CURRENT.reset(token)
            self._save_span(record)
            with suppress(Exception):
                export_span(record)

    def _save_span(self, record: dict[str, Any]) -> None:
        with suppress(sqlite3.Error, OSError), self._db() as db:
            db.execute(
                "INSERT INTO invocation_spans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(span_id) DO UPDATE SET status=excluded.status, "
                "ended_ns=excluded.ended_ns,duration=excluded.duration,metadata=excluded.metadata",
                (
                    *tuple(
                        record[k]
                        for k in (
                            "span_id",
                            "trace_id",
                            "tenant",
                            "run_id",
                            "step_id",
                            "attempt",
                            "operation",
                            "name",
                            "parent_span_id",
                            "status",
                            "started_ns",
                            "ended_ns",
                            "duration",
                        )
                    ),
                    json.dumps(record["metadata"], allow_nan=False),
                ),
            )

    def call(
        self,
        operation: str,
        name: str,
        function: Callable[..., T],
        *args: Any,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> T:
        # metadata is reserved by the tracing wrapper. Functions with a metadata
        # argument can be passed as a lambda/partial, without API ambiguity.
        with self.observe(operation, name, metadata=metadata) as span:
            if span:
                span["metadata"].update(_fingerprint({"args": args, "kwargs": kwargs}, "input"))
            result = function(*args, **kwargs)
            if span:
                span["metadata"].update(_fingerprint(result, "output"))
                if operation == "model" and isinstance(result, dict):
                    runtime = result.get("_runtime", {})
                    if isinstance(runtime, dict):
                        span["metadata"].update(safe_fields(runtime))
            return result

    def spans(
        self,
        tenant: str,
        run_id: str | None = None,
        limit: int = 1000,
        *,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM invocation_spans WHERE tenant=?", [tenant]
        if run_id:
            query, args = query + " AND run_id=?", [*args, run_id]
        if trace_id:
            query, args = query + " AND trace_id=?", [*args, trace_id]
        query += " ORDER BY started_ns DESC LIMIT ?"
        with self._db() as db:
            rows = db.execute(query, [*args, min(10000, max(1, limit))]).fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in reversed(rows)]

    def find_run_by_trace_id(self, tenant: str, trace: str) -> str | None:
        with self._db() as db:
            row = db.execute(
                "SELECT run_id FROM invocation_spans WHERE tenant=? AND trace_id=? LIMIT 1",
                (tenant, trace),
            ).fetchone()
        return str(row[0]) if row else None

    def diagnostics(self, tenant: str, run_id: str) -> dict[str, Any]:
        spans = self.spans(tenant, run_id, limit=10000)
        with self._db() as db:
            count = db.execute(
                "SELECT count(*) FROM invocation_spans WHERE tenant=? AND run_id=?",
                (tenant, run_id),
            ).fetchone()[0]
        model = [s for s in spans if s["operation"] == "model"]
        tools = [s for s in spans if s["operation"] in {"tool", "retrieval"}]
        agents = [s for s in spans if s["operation"] == "agent"]
        attempts_by_step: dict[str, int] = {}
        for span in agents:
            attempts_by_step[span["step_id"]] = attempts_by_step.get(span["step_id"], 0) + 1
        failed = [s for s in spans if s["status"] == "failed"]
        durations = sorted(s["duration"] for s in model if s["duration"] is not None)
        usage: dict[str, int] = {}
        for span in model:
            for key, value in span["metadata"].get("usage", {}).items():
                usage[key] = usage.get(key, 0) + value
        return {
            "trace_id": trace_id(tenant, run_id),
            "run_id": run_id,
            "span_count": count,
            "model_calls": len(model),
            "tool_calls": len(tools),
            "failed_calls": sum(s["operation"] != "agent" for s in failed),
            "failed_attempts": sum(s["status"] == "failed" for s in agents),
            "deferred_calls": sum(s["status"] == "deferred" for s in [*model, *tools]),
            "deferred_attempts": sum(s["status"] == "deferred" for s in agents),
            "usage": usage,
            "incomplete": any(s["status"] == "running" for s in spans),
            "truncated": count > len(spans),
            "retry_attempts": sum(max(0, count - 1) for count in attempts_by_step.values()),
            "attempt_count": len(agents),
            "retried_steps": sum(count > 1 for count in attempts_by_step.values()),
            "max_lease_token": max((s["attempt"] for s in agents), default=0),
            "model_latency_p95_seconds": durations[max(0, math.ceil(len(durations) * 0.95) - 1)]
            if durations
            else None,
            "slowest": sorted(
                (s for s in spans if s["operation"] != "agent"),
                key=lambda s: s["duration"] or 0,
                reverse=True,
            )[:5],
            "errors": [
                {
                    "span_id": s["span_id"],
                    "name": s["name"],
                    "step_id": s["step_id"],
                    "attempt": s["attempt"],
                    "error_type": s["metadata"].get("error_type"),
                    "error_code": s["metadata"].get("error_code"),
                    "http_status": s["metadata"].get("http_status"),
                }
                for s in failed
            ],
            "content_policy": "hashes-and-allowlisted-metadata-only",
        }

    def span_metrics(self, tenant: str) -> str:
        with self._db() as db:
            rows = db.execute(
                "SELECT operation,name,status,count(*) n,sum(coalesce(duration,0)) seconds "
                "FROM invocation_spans WHERE tenant=? GROUP BY operation,name,status",
                (tenant,),
            ).fetchall()
            usage = db.execute(
                "SELECT sum(coalesce(json_extract(metadata,'$.usage.input_tokens'),0)) inputs, "
                "sum(coalesce(json_extract(metadata,'$.usage.output_tokens'),0)) outputs "
                "FROM invocation_spans WHERE tenant=? AND operation='model'",
                (tenant,),
            ).fetchone()
        lines = [
            "# TYPE qt_agent_invocations_total counter",
            "# TYPE qt_agent_invocation_duration_seconds_sum counter",
            "# TYPE qt_agent_model_tokens_total counter",
        ]
        for row in rows:
            labels = f'operation="{row["operation"]}",name="{row["name"]}",status="{row["status"]}"'
            lines.append(f"qt_agent_invocations_total{{{labels}}} {row['n']}")
            lines.append(f"qt_agent_invocation_duration_seconds_sum{{{labels}}} {row['seconds']}")
        for key in ("inputs", "outputs"):
            lines.append(f'qt_agent_model_tokens_total{{direction="{key}"}} {usage[key] or 0}')
        return "\n".join(lines) + "\n"
