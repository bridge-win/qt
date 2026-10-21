"""Run-scoped evidence receipts, proposal binding and redacted execution traces.

Receipts prove which bytes were supplied, not that an assertion is true. Model
quality and market performance require the separate evaluation/validation gates.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from qt.agent_hub.context import identifier
from qt.agent_hub.observability import safe_fields
from qt.agent_hub.tracing import InvocationLedger


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class Governance(InvocationLedger):
    def __init__(self, root: Path) -> None:
        self.path = root / "governance.sqlite3"
        root.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evidence_receipts (
                    tenant TEXT NOT NULL, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
                    receipt_hash TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(tenant,step_id,receipt_hash));
                CREATE TABLE IF NOT EXISTS execution_traces (
                    id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                    duration REAL NOT NULL, metadata TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS trace_run ON execution_traces(tenant,run_id,id);
            """)
            self.initialize_spans(db)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def receipt(
        self, tenant: str, run_id: str, step_id: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        identifier(tenant)
        references = []
        for item in context.get("references", []):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                references.append({k: item[k] for k in ("id", "sha256", "source") if k in item})
        payload = {
            "run_id": run_id,
            "step_id": step_id,
            "references": references,
            "context_sha256": digest(context),
            "created_at": time.time(),
        }
        receipt_hash = digest(payload)
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO evidence_receipts VALUES(?,?,?,?,?,?)",
                (
                    tenant,
                    run_id,
                    step_id,
                    receipt_hash,
                    json.dumps(payload, ensure_ascii=False),
                    payload["created_at"],
                ),
            )
        return {**payload, "receipt_hash": receipt_hash}

    def validate(self, tenant: str, receipt: dict[str, Any], citations: object) -> dict[str, Any]:
        with self._db() as db:
            saved = db.execute(
                "SELECT payload FROM evidence_receipts WHERE tenant=? AND step_id=? AND receipt_hash=?",
                (tenant, receipt.get("step_id"), receipt.get("receipt_hash")),
            ).fetchone()
        errors = []
        if not saved or digest(json.loads(saved[0])) != receipt.get("receipt_hash"):
            errors.append("receipt_not_found_or_changed")
        elif {key: value for key, value in receipt.items() if key != "receipt_hash"} != json.loads(
            saved[0]
        ):
            errors.append("receipt_payload_changed")
        allowed = {item["id"] for item in json.loads(saved[0])["references"]} if saved else set()
        if not isinstance(citations, list) or not citations:
            errors.append("missing_evidence")
        elif any(not isinstance(item, str) or item not in allowed for item in citations):
            errors.append("evidence_not_in_supplied_snapshot")
        return {"valid": not errors, "errors": errors, "receipt_hash": receipt.get("receipt_hash")}

    def trace(
        self,
        tenant: str,
        run_id: str,
        step_id: str,
        kind: str,
        status: str,
        duration: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        # Deliberate allowlist: no prompt, credential, raw exception, or reasoning.
        fields = safe_fields(metadata)
        usage = fields.get("usage")
        if isinstance(usage, dict):
            fields["usage"] = {
                key: value
                for key, value in usage.items()
                if key
                in {
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                }
                and type(value) is int
                and 0 <= value <= 10**12
            }
        else:
            fields.pop("usage", None)
        with self._db() as db:
            db.execute(
                "INSERT INTO execution_traces VALUES(NULL,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    run_id,
                    step_id,
                    kind,
                    status,
                    max(0, duration),
                    json.dumps(fields, allow_nan=False),
                    time.time(),
                ),
            )

        # Export only after the mandatory local transaction commits. Exporter
        # failures cannot turn a completed business step into a retry.
        with suppress(Exception):
            from qt.agent_hub.observability import export_step

            with self._db() as db:
                instrumented = db.execute(
                    "SELECT 1 FROM invocation_spans WHERE tenant=? AND run_id=? "
                    "AND step_id=? LIMIT 1",
                    (tenant, run_id, step_id),
                ).fetchone()
            if not instrumented:
                export_step(tenant, run_id, step_id, kind, status, duration, fields)

    def traces(
        self, tenant: str, run_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM execution_traces WHERE tenant=? "
                + ("AND run_id=? " if run_id else "")
                + "ORDER BY id DESC LIMIT ?",
                (tenant, run_id, min(1000, max(1, limit)))
                if run_id
                else (tenant, min(1000, max(1, limit))),
            ).fetchall()
        return [
            {
                **dict(row),
                "metadata": json.loads(row["metadata"]),
                "trace_id": digest([tenant, row["run_id"]])[:32],
            }
            for row in rows
        ]

    def metrics(self, tenant: str) -> str:
        with self._db() as db:
            rows = db.execute(
                "SELECT kind,status,count(*) n,sum(duration) seconds FROM execution_traces "
                "WHERE tenant=? GROUP BY kind,status",
                (tenant,),
            ).fetchall()
        lines = [
            "# TYPE qt_agent_steps_total counter",
            "# TYPE qt_agent_duration_seconds_sum counter",
        ]
        for row in rows:
            labels = f'kind="{row["kind"]}",status="{row["status"]}"'
            lines += [
                f"qt_agent_steps_total{{{labels}}} {row['n']}",
                f"qt_agent_duration_seconds_sum{{{labels}}} {row['seconds']}",
            ]
        return "\n".join(lines) + "\n" + self.span_metrics(tenant)


def attach_tool_evidence(context: dict[str, Any]) -> None:
    """Give deterministic tool results content-addressed IDs that models can cite."""
    references = list(context.get("references", []))
    for key in (
        "market",
        "market_history",
        "experiment",
        "verified_backtest",
        "public_sources",
        "opportunities",
        "selected_opportunity",
    ):
        value = context.get(key)
        if not value or (isinstance(value, dict) and value.get("ready") is False):
            continue
        sha = digest(value)
        references.append({"id": f"{key}:{sha[:24]}", "sha256": sha, "source": key})
    context["references"] = references


def proposal(
    forecast: dict[str, Any], experiment: dict[str, Any] | None, config_hash: str
) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "venue": forecast.get("venue"),
        "instrument": forecast.get("instrument"),
        "outcome_token": forecast.get("outcome_token"),
        "answer": {
            key: forecast["answer"].get(key)
            for key in (
                "decision",
                "confidence",
                "evidence",
                "probability",
                "side",
                "horizon_seconds",
            )
        },
        "quote": forecast.get("quote"),
        "evidence_receipt": forecast.get("evidence_receipt", {}).get("receipt_hash"),
        "validation_hash": digest(experiment) if experiment else None,
        "policy_hash": config_hash,
    }
    return {**value, "proposal_hash": digest(value)}
