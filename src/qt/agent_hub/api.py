"""Loopback personal API. Tenant identity is server-owned, never a request field."""

from __future__ import annotations

import hmac
import json
import os
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qt.agent_hub.config import ROLES, HubConfig
from qt.agent_hub.context import _safe_path
from qt.agent_hub.runtime import HubRuntime


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "pipeline",
        "discover",
        "research",
        "analysis",
        "backtest",
        "forecast",
        "risk_review",
        "log_review",
        "monitor",
        "import_context",
    ] = "pipeline"
    question: str = Field(default="Review current BTC evidence and uncertainty.", max_length=8000)
    venue: Literal["binance", "polymarket"] = "binance"
    instrument: str = Field(default="BTCUSDT", min_length=1, max_length=200)
    outcome_token: str | None = Field(default=None, max_length=78)
    strategy: Literal["ma_trend"] = "ma_trend"
    auto_execute: bool = False
    candidate_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_scan_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    idempotency_key: str = Field(
        default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=200
    )

    @model_validator(mode="after")
    def candidate_identity(self):
        if (self.candidate_id is None) != (self.source_scan_id is None):
            raise ValueError("candidate_id_and_source_scan_id_required_together")
        return self


class DocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source: str = Field(min_length=1, max_length=2048)
    content: str = Field(min_length=1, max_length=200000)
    valid_from: float | None = None
    valid_until: float | None = None
    available_at: float | None = None
    published_at: float | None = None
    provenance: str | None = Field(default=None, max_length=2048)


class MemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=16000)
    scope: Literal["short", "long"] = "long"
    ttl_seconds: int | None = Field(default=None, ge=1, le=31536000)
    source: str | None = Field(default=None, max_length=2000)


class KillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["once", "class", "reject"]


class RiskModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["active", "reduce_only", "frozen"]


class OpportunityReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["watching", "rejected", "archived"]
    note: str = Field(default="", max_length=2000)


def create_app(config: HubConfig, root: Path, files: Path, *, runtime=None) -> FastAPI:
    service = runtime or HubRuntime(config, root, files)
    tenant = config.tenant_id
    app = FastAPI(
        title="QT Agent Hub", version="2.1.0", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.runtime = service

    def authenticated(authorization: str = Header(default="")) -> None:
        token = os.environ.get("QT_AGENT_HUB_SERVICE_TOKEN", "")
        if not token or not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "local access token required")

    auth = [Depends(authenticated)]

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        hub_port = os.environ.get("QT_AGENT_HUB_PORT", "8878")
        allowed = {
            f"http://127.0.0.1:{hub_port}",
            f"http://localhost:{hub_port}",
            # The container always listens on 8878, independently of the host
            # published port. Internal same-network browser acceptance uses it.
            "http://127.0.0.1:8878",
            "http://localhost:8878",
            "http://127.0.0.1:8877",
            "http://localhost:8877",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        }
        if origin and origin not in allowed:
            return JSONResponse({"detail": "untrusted origin"}, status_code=403)
        if request.method == "OPTIONS" and origin in allowed:
            preflight = Response(status_code=204)
            preflight.headers["Access-Control-Allow-Origin"] = origin
            preflight.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Last-Event-ID"
            preflight.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            preflight.headers["Access-Control-Max-Age"] = "600"
            return preflight
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "invalid request length"}, status_code=400)
        if size > 1000000 or len(await request.body()) > 1000000:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        if origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Last-Event-ID"
            )
        return response

    @app.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError):
        return JSONResponse({"detail": "invalid request or operation state"}, status_code=422)

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True, "service": "qt-agent-hub"}

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).with_name("static") / "index.html")

    @app.get("/api/status", dependencies=auth)
    def status() -> dict:
        beats = []
        for source in sorted((root / "heartbeats").glob("*.json")):
            try:
                item = json.loads(source.read_text())
                item["age_seconds"] = round(time.time() - item["timestamp"], 1)
                item["healthy"] = item["age_seconds"] < 60
                beats.append(item)
            except (ValueError, OSError, KeyError):
                continue
        return {
            "tenant_id": tenant,
            "mode": config.mode,
            "auto_execute": config.auto_execute,
            "queue": service.store.summary(tenant),
            "workers": beats,
            "trading": service.trading.status(tenant),
            "provider": "local-codex",
            "roles": list(ROLES),
            "limits": {
                "max_order_quote": config.max_order_quote,
                "max_position_quote": config.max_position_quote,
                "max_daily_quote": config.max_daily_quote,
            },
            "retrieval": "SQLite FTS5/BM25 with CJK fallback; no embedding model",
            "sandbox": "isolated fixed-function numerical executor",
            "version": "2.1.0",
        }

    @app.get("/api/capabilities", dependencies=auth)
    def capabilities() -> dict:
        return {
            "version": "2.1.0",
            "deployment": "personal-single-host-docker",
            "provider": "local-codex-online-inference-serialized-shared-auth",
            "workflow": "durable-six-role-workflow-with-fenced-execution",
            "opportunities": "public-btc-and-prediction-market-screens-with-immutable-snapshots-no-trading-approval",
            "observability": "persistent-agent-model-tool-retrieval-spans-and-optional-langfuse-otlp",
            "evidence": "run-receipts-version-hash-tenant-and-time-validation",
            "memory": "versioned-short-long-with-ttl-and-historical-views",
            "retrieval": "lexical-fts5-cjk-and-authorized-bounded-chunk-readback",
            "skills": "server-owned-versioned-manifests-with-domain-tool-allowlists",
            "strategy": "ma_trend",
            "execution": ["paper", "testnet-adapter", "live-adapter"],
            "risk_modes": ["active", "reduce_only", "frozen"],
            "mcp": {
                "protocol": "2026-07-28",
                "legacy_protocol": "2025-11-25",
                "scope": "authenticated-json-http-discovery-and-three-tools",
                "unsupported": ["tasks", "subscriptions", "oauth-discovery", "a2a"],
            },
            "unverified_or_unimplemented": [
                "funded-live-account-acceptance",
                "multi-tenant-identity-and-database-rls",
                "arbitrary-generated-code-sandbox",
                "semantic-embedding-retrieval",
                "independently-demonstrated-predictive-alpha",
                "cloud-disaster-recovery",
            ],
        }

    @app.get("/api/skills", dependencies=auth)
    def skills() -> dict:
        return {"items": service.skills.list()}

    @app.get("/api/traces", dependencies=auth)
    def traces(
        run_id: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict:
        return {"items": service.governance.traces(tenant, run_id=run_id, limit=limit)}

    @app.get("/api/release/readiness", dependencies=auth)
    def release_readiness() -> dict:
        from qt.agent_hub.release import readiness

        return readiness(service)

    @app.get("/api/runs/{run_id}", dependencies=auth)
    def run_details(run_id: str) -> dict:
        jobs = service.store.run_jobs(tenant, run_id)
        if not jobs:
            raise HTTPException(404, "run not found")
        return {"run_id": run_id, "items": jobs[:1000], "truncated": len(jobs) > 1000}

    @app.get("/api/runs/{run_id}/diagnostics", dependencies=auth)
    def run_diagnostics(run_id: str) -> dict:
        jobs = service.store.run_jobs(tenant, run_id)
        if not jobs:
            raise HTTPException(404, "run not found")
        report = service.governance.diagnostics(tenant, run_id)
        report["jobs_truncated"] = len(jobs) > 1000
        report["steps"] = [
            {
                "id": item["id"],
                "kind": item["kind"],
                "status": item["status"],
                "attempts": item["attempts"],
                "error_type": item.get("error"),
                "outcome": (item.get("result") or {}).get("status"),
                "reason": (item.get("result") or {}).get("reason"),
                "evidence_valid": (item.get("result") or {})
                .get("evidence_validation", {})
                .get("valid"),
                "release_sha256": (item.get("result") or {}).get("release_sha256"),
            }
            for item in jobs[:1000]
        ]
        return report

    @app.get("/api/spans", dependencies=auth)
    def spans(
        run_id: str | None = Query(default=None, min_length=1, max_length=128),
        trace_id: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
        limit: int = Query(default=300, ge=1, le=1000),
    ) -> dict:
        if not run_id and not trace_id:
            raise HTTPException(422, "run_id or trace_id required")
        return {
            "items": service.governance.spans(tenant, run_id=run_id, trace_id=trace_id, limit=limit)
        }

    @app.get("/api/trace/{trace_id}/diagnostics", dependencies=auth)
    def trace_diagnostics(trace_id: str) -> dict:
        run_id = service.governance.find_run_by_trace_id(tenant, trace_id)
        if run_id is None:
            raise HTTPException(404, "trace not found")
        return run_diagnostics(run_id)

    @app.get("/api/opportunities", dependencies=auth)
    def opportunities() -> dict:
        return service.opportunities.summary(tenant)

    @app.get("/api/opportunities/scans/{scan_id}", dependencies=auth)
    def opportunity_scan(scan_id: str) -> dict:
        result = service.opportunities.scan_detail(tenant, scan_id)
        if result is None:
            raise HTTPException(404, "scan not found")
        return result

    @app.post("/api/opportunities/{candidate_id}/review", dependencies=auth)
    def opportunity_review(candidate_id: str, body: OpportunityReviewRequest) -> dict:
        if service.opportunities.get(tenant, candidate_id) is None:
            raise HTTPException(404, "candidate not found")
        return service.opportunities.review(tenant, candidate_id, **body.model_dump())

    @app.get("/api/roles", dependencies=auth)
    def roles() -> dict:
        return {"items": {name: value.model_dump() for name, value in config.roles.items()}}

    @app.get("/api/jobs", dependencies=auth)
    def jobs() -> dict:
        return {"items": service.store.list_jobs(tenant)}

    @app.post("/api/jobs", dependencies=auth, status_code=202)
    def enqueue(body: JobRequest) -> dict:
        payload = body.model_dump(exclude={"kind", "idempotency_key"})
        if body.candidate_id is not None:
            snapshot = service.opportunities.scan_detail(tenant, body.source_scan_id)
            selected = next(
                (
                    item
                    for item in (snapshot or {}).get("candidates", [])
                    if item["id"] == body.candidate_id
                ),
                None,
            )
            if selected is None:
                raise HTTPException(404, "candidate observation not found")
            target = body.outcome_token if body.venue == "polymarket" else body.instrument
            if selected["venue"] != body.venue or selected["instrument"] != target:
                raise HTTPException(422, "candidate does not match requested instrument")
            payload["selected_candidate"] = {
                **selected,
                "source_scan_id": body.source_scan_id,
                "source_scan_sha256": snapshot["sha256"],
            }
        return service.store.enqueue(
            tenant, body.kind, payload, idempotency_key=body.idempotency_key
        )

    @app.get("/api/jobs/{job_id}", dependencies=auth)
    def job(job_id: str) -> dict:
        item = service.store.get_job(tenant, job_id)
        if item is None:
            raise HTTPException(404, "job not found")
        return {**item, "events": service.store.events(tenant, job_id)}

    @app.post("/api/jobs/{job_id}/retry", dependencies=auth)
    def retry(job_id: str) -> dict:
        try:
            return service.store.requeue_deadletter(tenant, job_id)
        except KeyError:
            raise HTTPException(404, "job not found") from None

    @app.post("/api/jobs/{job_id}/cancel", dependencies=auth)
    def cancel(job_id: str) -> dict:
        if service.store.get_job(tenant, job_id) is None:
            raise HTTPException(404, "job not found")
        return service.store.cancel_run(tenant, job_id)

    @app.get("/api/schedules", dependencies=auth)
    def schedules() -> dict:
        return {"items": service.store.list_schedules(tenant)}

    @app.get("/api/events", dependencies=auth)
    def events(
        after_id: int | None = Query(default=None, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> dict:
        cursor = after_id
        if cursor is None and last_event_id and last_event_id.isdigit():
            cursor = int(last_event_id)
        return {"items": service.store.events(tenant, limit=100, after_id=cursor)}

    @app.get("/api/inbox", dependencies=auth)
    def inbox() -> dict:
        schedules = service.store.list_schedules(tenant)
        due = min((item["next_run_at"] for item in schedules if item.get("enabled")), default=None)
        failed = [
            item
            for item in service.store.list_jobs(tenant, limit=50)
            if item["status"] == "deadletter"
        ]
        now = time.time()
        return {
            "approvals": service.approvals.pending(tenant),
            "failed_jobs": failed[:20],
            "next_run_at": due,
            "next_run_in_seconds": None if due is None else max(0, due - now),
            "timezone": "UTC",
            "empty_reason": None
            if due is None or failed or service.approvals.pending(tenant)
            else f"下次运行 {max(0, int(due - now))} 秒后",
        }

    @app.get("/api/approvals", dependencies=auth)
    def approvals() -> dict:
        return {"items": service.approvals.pending(tenant)}

    @app.post("/api/approvals/{approval_id}", dependencies=auth)
    def decide_approval(approval_id: str, body: ApprovalDecision) -> dict:
        try:
            item = service.approvals.decide(tenant, approval_id, body.decision)
        except KeyError:
            raise HTTPException(404, "approval not found") from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if body.decision != "reject" and isinstance(item.get("payload"), dict):
            intent = item["payload"]
            if intent.get("idempotency_key"):
                submitted = service.trading.submit(tenant, intent)
                service.trading.record_effect(
                    tenant,
                    str(intent["idempotency_key"]),
                    status=str(submitted.get("status") or "submitted"),
                    external_id=submitted.get("order_id") or submitted.get("exchange_id"),
                    result={"status": submitted.get("status")},
                )
                item["order"] = submitted
        return item

    @app.get("/api/forecasts", dependencies=auth)
    def forecasts() -> dict:
        return service.forecasts.score(tenant)

    @app.get("/api/trials", dependencies=auth)
    def trials() -> dict:
        return {"count": service.trials.count(tenant)}

    @app.post("/api/documents", dependencies=auth)
    def documents(body: DocumentRequest) -> dict:
        return service.context.ingest(
            tenant,
            body.source,
            body.content,
            valid_from=body.valid_from,
            valid_until=body.valid_until,
            available_at=body.available_at,
            published_at=body.published_at,
            provenance=body.provenance,
        )

    @app.get("/api/search", dependencies=auth)
    def search(q: str, as_of: float | None = None, known_at: float | None = None) -> dict:
        return {"items": service.context.search(tenant, q, as_of=as_of, known_at=known_at)}

    @app.get("/api/memories", dependencies=auth)
    def memories(as_of: float | None = None, known_at: float | None = None) -> dict:
        return {"items": service.context.memories(tenant, as_of=as_of, known_at=known_at)}

    @app.post("/api/memories", dependencies=auth)
    def remember(body: MemoryRequest) -> dict:
        return service.context.remember(
            tenant,
            body.key,
            body.value,
            scope=body.scope,
            ttl_seconds=body.ttl_seconds,
            source=body.source,
        )

    @app.delete("/api/memories/{key}", dependencies=auth)
    def forget(key: str) -> dict:
        return {"deleted": service.context.forget(tenant, key)}

    @app.get("/api/quote", dependencies=auth)
    def quote(venue: str = "binance", instrument: str = "BTCUSDT") -> dict:
        try:
            return service.trading.quote(venue, instrument)
        except Exception:
            raise HTTPException(503, "public market data unavailable") from None

    @app.get("/api/prediction-markets", dependencies=auth)
    def prediction_markets(q: str = "bitcoin") -> dict:
        try:
            return {"items": service.trading.discover_markets(q)}
        except Exception:
            raise HTTPException(503, "prediction market discovery unavailable") from None

    @app.get("/api/orders", dependencies=auth)
    def orders() -> dict:
        return {"items": service.trading.orders(tenant)}

    @app.get("/api/orders/{order_id}", dependencies=auth)
    def order(order_id: str) -> dict:
        item = service.trading.get_order(tenant, order_id)
        if item is None:
            raise HTTPException(404, "order not found")
        return item

    @app.get("/api/portfolio", dependencies=auth)
    def portfolio() -> dict:
        return service.trading.portfolio(tenant)

    @app.post("/api/orders", dependencies=auth)
    def submit(intent: dict) -> dict:
        # The authenticated local owner can submit a typed intent. TradingService
        # still enforces execution mode, balances, quote freshness and risk limits.
        if "tenant_id" in intent:
            raise HTTPException(422, "tenant identity is server-owned")
        return service.trading.submit(tenant, intent)

    @app.post("/api/reconcile", dependencies=auth)
    def reconcile() -> dict:
        return service.trading.reconcile(tenant)

    @app.post("/api/kill", dependencies=auth)
    def kill(body: KillRequest) -> dict:
        service.trading.set_kill(tenant, body.enabled)
        return service.trading.status(tenant)

    @app.post("/api/risk-mode", dependencies=auth)
    def risk_mode(body: RiskModeRequest) -> dict:
        return service.trading.set_risk_mode(tenant, body.mode)

    @app.get("/api/artifacts/{name}", dependencies=auth)
    def artifact(name: str):
        if (
            len(name) > 128
            or not name.endswith(".json")
            or any(char not in "0123456789abcdef-.json" for char in name)
        ):
            raise HTTPException(404, "artifact not found")
        try:
            base = _safe_path(files.resolve(), tenant, "artifacts")
        except ValueError:
            raise HTTPException(404, "artifact not found") from None
        target = base / name
        if target.is_symlink() or target.resolve().parent != base or not target.is_file():
            raise HTTPException(404, "artifact not found")
        return FileResponse(target, media_type="application/json", filename=name)

    @app.get("/metrics", dependencies=auth)
    def metrics():
        summary = service.store.summary(tenant)
        trading = service.trading.status(tenant)
        lines = ["# TYPE qt_agent_hub_jobs gauge"]
        lines += [
            f'qt_agent_hub_jobs{{status="{key}"}} {value}'
            for key, value in summary["counts"].items()
        ]
        lines += [
            "# TYPE qt_agent_hub_unknown_orders gauge",
            f"qt_agent_hub_unknown_orders {trading.get('unresolved_orders', 0)}",
            "# TYPE qt_agent_hub_kill_enabled gauge",
            f"qt_agent_hub_kill_enabled {int(bool(trading.get('kill_enabled')))}",
        ]
        return PlainTextResponse(
            "\n".join(lines) + "\n" + service.governance.metrics(tenant),
            media_type="text/plain; version=0.0.4",
        )

    @app.post("/mcp", dependencies=auth)
    async def mcp(request: Request):
        from qt.agent_hub.mcp import dispatch, error

        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            return error(None, -32700, "Invalid JSON")
        return dispatch(
            body,
            request.headers,
            search=lambda query: service.context.search(tenant, query),
            status=status,
            submit_research=lambda question: enqueue(
                JobRequest(question=question, auto_execute=False)
            ),
        )

    return app
