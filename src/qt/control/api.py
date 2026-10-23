"""Authenticated V4 routes; all financial side effects remain worker-owned."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from qt.control.service import ControlService
from qt.persistence import Conflict, Repository, create_engine


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    question: str = Field(min_length=1, max_length=8000)
    maximum_cost_usd: str = Field(default="1", pattern=r"^(?:0\.(?:0[1-9]|[1-9][0-9]?)|1(?:\.0{1,2})?)$")
    deadline_seconds: int = Field(default=900, ge=10, le=900)


class ValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    stage: Literal["research", "paper", "shadow", "live_canary", "scale"] = "paper"


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["ACTIVE", "REDUCE_ONLY", "HALTED", "EMERGENCY_EXIT"]
    reason: str = Field(min_length=1, max_length=2000)


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1024)


class RollbackRequest(ReleaseRequest):
    valid_until_ns: int = Field(gt=0)


class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1, max_length=120)
    dataset_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    holdout_start_ns: int = Field(gt=0)
    holdout_end_ns: int = Field(gt=0)
    protocol: dict
    candidate_limit: int = Field(default=30, ge=1, le=30)


def build_router(service: ControlService, *, dependencies=None) -> APIRouter:
    router = APIRouter(prefix="/api/v4", dependencies=dependencies or [])
    repo, tenant = service.repository, service.tenant

    @router.get("/readiness")
    def readiness():
        return service.readiness()

    @router.get("/policy")
    def policy():
        return {**service.policy.definition(), "policy_hash": service.policy.fingerprint()}

    @router.post("/research-runs", status_code=202)
    def research(body: ResearchRequest, idempotency_key: str = Header(alias="Idempotency-Key")):
        model_id = os.environ.get("QT_V4_MODEL_ID")
        if not model_id:
            raise HTTPException(503, "Explicit QT_V4_MODEL_ID is required")
        return repo.enqueue(tenant, "research", {**body.model_dump(), "model_id": model_id},
                            idempotency_key,
                            deadline_ns=time.time_ns() + body.deadline_seconds * 1_000_000_000)

    @router.get("/research-runs/{run_id}")
    def run(run_id: str):
        return repo.job(tenant, run_id)

    @router.post("/research-runs/{run_id}/cancel")
    def cancel(run_id: str):
        return repo.cancel(tenant, run_id)

    @router.get("/research-runs/{run_id}/events")
    async def stream(request: Request, run_id: str, after: int = Query(default=0, ge=0),
                     follow: bool = False, last_event_id: str | None = Header(default=None)):
        repo.job(tenant, run_id)
        try:
            cursor = int(last_event_id) if last_event_id is not None else after
        except ValueError:
            raise HTTPException(422, "invalid_event_cursor") from None
        if cursor < 0:
            raise HTTPException(422, "invalid_event_cursor")

        async def generate():
            nonlocal cursor
            until = time.monotonic() + 60
            while not await request.is_disconnected():
                page = await asyncio.to_thread(repo.event_page, tenant, run_id, after=cursor)
                for event in page:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event, allow_nan=False)}\n\n"
                if not follow or time.monotonic() >= until:
                    break
                if repo.job(tenant, run_id)["status"] not in {"QUEUED", "RUNNING"} and not page:
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.post("/strategy-versions", status_code=201)
    def strategy(body: dict):
        return service.register("strategy", body)

    @router.get("/strategy-versions")
    def strategies(limit: int = Query(default=100, ge=1, le=200)):
        return {"items": repo.list(tenant, "strategy", limit=limit)}

    @router.post("/instruments", status_code=201)
    def instrument(body: dict):
        return service.register("instrument", body)

    @router.post("/fee-schedules", status_code=201)
    def fees(body: dict):
        return service.register("fees", body)

    @router.post("/datasets", status_code=201)
    def dataset(body: dict):
        # Registration accepts a verified manifest, never a client-chosen path.
        return service.register("dataset", body)

    @router.get("/datasets")
    def datasets(limit: int = Query(default=100, ge=1, le=200)):
        return {"items": repo.list(tenant, "dataset", limit=limit)}

    @router.post("/campaigns", status_code=201)
    def campaign(body: CampaignRequest):
        repo.get(tenant, "dataset", body.dataset_id)
        payload = body.model_dump()
        identity = payload.pop("campaign_id")
        return service.experiments.register_campaign(tenant, identity, **payload)

    @router.post("/experiments", status_code=202)
    def experiment(body: dict, idempotency_key: str = Header(alias="Idempotency-Key")):
        return service.create_experiment(body, idempotency_key)

    @router.get("/experiments")
    def experiments(limit: int = Query(default=100, ge=1, le=200)):
        return {"items": repo.list(tenant, "experiment_result", limit=limit)}

    @router.get("/experiments/{experiment_hash}")
    def experiment_result(experiment_hash: str):
        return repo.get(tenant, "experiment_result", experiment_hash)

    @router.post("/validations", status_code=201)
    def validate(body: ValidationRequest):
        return service.save_validation(body.experiment_hash, body.stage)

    @router.post("/deployments", status_code=201)
    def deploy(body: dict, idempotency_key: str = Header(alias="Idempotency-Key")):
        return service.create_deployment(body, idempotency_key)

    @router.get("/deployments")
    def deployments():
        return {"items": repo.list(tenant, "deployment"), "active": service.active_deployments()}

    @router.post("/deployments/{deployment_id}/deactivate")
    def deactivate(deployment_id: str, body: ReleaseRequest):
        return service.deactivate_deployment(deployment_id, reason=body.reason)

    @router.post("/deployments/{deployment_id}/rollback", status_code=201)
    def rollback(deployment_id: str, body: RollbackRequest,
                  idempotency_key: str = Header(alias="Idempotency-Key")):
        return service.rollback_deployment(deployment_id, idempotency_key, reason=body.reason,
                                             valid_until_ns=body.valid_until_ns)

    @router.post("/deployments/{deployment_id}/mode")
    def risk_mode(deployment_id: str, body: ModeRequest):
        from qt.execution.oms import OMS

        deployment = service.verify_deployment(deployment_id)
        return JSONResponse(jsonable_encoder(
            OMS(repo).set_mode(tenant, deployment.account_id, body.mode, body.reason),
            custom_encoder={Decimal: str}))

    @router.get("/orders/{intent_id}")
    def order(intent_id: str):
        from qt.execution.oms import OMS

        return JSONResponse(jsonable_encoder(OMS(repo).get_order(tenant, intent_id),
                                              custom_encoder={Decimal: str}))

    @router.post("/reconciliation-runs", status_code=202)
    def reconciliation(body: dict, idempotency_key: str = Header(alias="Idempotency-Key")):
        if set(body) != {"account_id"} or not isinstance(body["account_id"], str):
            raise ValueError("account_id_required")
        from qt.control.worker import paper_reconciliation_deployment

        deployment = paper_reconciliation_deployment(service, body["account_id"])
        # Queue-only. No resubmission authority or adapter comes from the caller.
        return repo.enqueue(tenant, "reconciliation", {
            "account_id": body["account_id"], "deployment_id": deployment.deployment_id,
        }, idempotency_key)

    @router.get("/forecasts/scorecards")
    def forecasts():
        from qt.forecasting import ForecastRepository

        return ForecastRepository(repo).score(tenant)

    @router.post("/forecasts", status_code=201)
    def forecast(body: dict):
        from qt.contracts import ForecastRecord
        from qt.control.service import parse
        from qt.forecasting import ForecastRepository

        return ForecastRepository(repo).record(tenant, parse(ForecastRecord, body))

    return router


def create_app(service: ControlService | None = None, *, token: str | None = None) -> FastAPI:
    expected = token or os.environ.get("QT_V4_SERVICE_TOKEN", "")
    if not expected:
        raise ValueError("QT_V4_SERVICE_TOKEN_required")
    if service is None:
        url = os.environ.get("QT_V4_DATABASE_URL", "")
        service = ControlService(Repository(create_engine(url)),
                                 Path(os.environ.get("QT_V4_DATA_ROOT", "/state/datasets")),
                                 signing_key=os.environ.get("QT_V4_SIGNING_KEY", "").encode())
    app = FastAPI(title="QT Production Control", version="4.0.0",
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    def authenticate(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, f"Bearer {expected}"):
            raise HTTPException(401, "authenticated_control_access_required")

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in {"http://localhost:8877", "http://127.0.0.1:8877",
                                     "http://localhost:8879", "http://127.0.0.1:8879",
                                     "http://localhost:8890", "http://127.0.0.1:8890"}:
            return JSONResponse({"detail": "untrusted_origin"}, status_code=403)
        if request.url.path.startswith("/api/v4") and not hmac.compare_digest(
                request.headers.get("authorization", ""), f"Bearer {expected}"):
            return JSONResponse({"detail": "authenticated_control_access_required"}, status_code=401)
        # Bound the stream before buffering/parsing. A length check after body()
        # would allow unbounded chunked requests to exhaust memory first.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1_000_000:
                return JSONResponse({"detail": "request_too_large"}, status_code=413)
            body.extend(chunk)
        # Starlette's cached-request middleware replays _body to the endpoint.
        request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(Conflict)
    async def conflict(_request, _error):
        return JSONResponse({"detail": "immutable_identity_or_state_conflict"}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_request, _error):
        return JSONResponse({"detail": "invalid_contract_or_acceptance_not_satisfied"}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing(_request, _error):
        return JSONResponse({"detail": "resource_not_found"}, status_code=404)

    @app.exception_handler(PermissionError)
    async def forbidden(_request, _error):
        return JSONResponse({"detail": "protected_holdout_or_capability"}, status_code=403)

    @app.get("/healthz")
    def health():
        return {"service": "qt-production-v4", "status": "process_running"}

    app.include_router(build_router(service, dependencies=[Depends(authenticate)]))
    return app
