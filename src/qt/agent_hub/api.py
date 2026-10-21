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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from qt.agent_hub.config import ROLES, HubConfig
from qt.agent_hub.context import _safe_path
from qt.agent_hub.runtime import HubRuntime


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "pipeline",
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
    idempotency_key: str = Field(
        default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=200
    )


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


class RiskModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["active", "reduce_only", "frozen"]


def create_app(config: HubConfig, root: Path, files: Path, *, runtime=None) -> FastAPI:
    service = runtime or HubRuntime(config, root, files)
    tenant = config.tenant_id
    app = FastAPI(
        title="QT Agent Hub", version="2.0.0", docs_url=None, redoc_url=None, openapi_url=None
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
        allowed = {
            f"http://127.0.0.1:{os.environ.get('QT_AGENT_HUB_PORT', '8878')}",
            f"http://localhost:{os.environ.get('QT_AGENT_HUB_PORT', '8878')}",
            # The container always listens on 8878, independently of the host
            # published port. Internal same-network browser acceptance uses it.
            "http://127.0.0.1:8878",
            "http://localhost:8878",
        }
        if origin and origin not in allowed:
            return JSONResponse({"detail": "untrusted origin"}, status_code=403)
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
            "version": "2.0.0",
        }

    @app.get("/api/capabilities", dependencies=auth)
    def capabilities() -> dict:
        return {
            "version": "2.0.0",
            "deployment": "personal-single-host-docker",
            "provider": "local-codex-online-inference-serialized-shared-auth",
            "workflow": "durable-six-role-workflow-with-fenced-execution",
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

    @app.get("/api/roles", dependencies=auth)
    def roles() -> dict:
        return {"items": {name: value.model_dump() for name, value in config.roles.items()}}

    @app.get("/api/jobs", dependencies=auth)
    def jobs() -> dict:
        return {"items": service.store.list_jobs(tenant)}

    @app.post("/api/jobs", dependencies=auth, status_code=202)
    def enqueue(body: JobRequest) -> dict:
        payload = body.model_dump(exclude={"kind", "idempotency_key"})
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
    def events() -> dict:
        return {"items": service.store.events(tenant, limit=100)}

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
        lines = ["# TYPE qt_agent_hub_jobs gauge"]
        lines += [
            f'qt_agent_hub_jobs{{status="{key}"}} {value}'
            for key, value in summary["counts"].items()
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
