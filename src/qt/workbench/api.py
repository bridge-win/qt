"""FastAPI v3 research workbench contract.

This service is intentionally research-only. It exposes real local catalog,
dataset and durable-job state; it never exposes a route that can enable live
trading or send an order.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint

from qt.lab.persistence import LabRepository
from qt.lab.router import LabSettings, build_lab_router
from qt.lab.service import LabExecutionAdapter, LabService, ParquetTimelineProvider
from qt.research.datasets import DatasetCatalog
from qt.research.repository import IdempotencyConflictError, ResearchRepository
from qt.workbench.auxiliary_datasets import (
    AuxiliaryDatasetCatalog,
    AuxiliaryReference,
    validate_auxiliary_selection,
)
from qt.workbench.capabilities import coverage_summary, manifest
from qt.workbench.catalog import catalog_summary, legacy_catalog, profile
from qt.workbench.inventory import capability_matrix, capability_summary, source_audit
from qt.workbench.operations import OperationsService, build_operations_router
from qt.workbench.resources import CapacityBlockedError, ResourcePolicy, available_memory_mib
from qt.workbench.source_research import RESEARCH_CONTRACT, build_source_research_report


@dataclass(frozen=True)
class WorkbenchSettings:
    parquet_root: Path
    state_root: Path
    artifact_root: Path | None = None
    queue_limit: int = 20
    resource_policy: ResourcePolicy = field(default_factory=ResourcePolicy)
    origin_client_id: str | None = None
    origin_client_secret: str | None = None


class ExperimentRequest(BaseModel):
    """Immutable-version experiment submission; the v3 API has no payload envelope."""

    model_config = ConfigDict(extra="forbid")
    strategy_version_id: str = Field(min_length=1, max_length=64)
    dataset_id: str = Field(min_length=1, max_length=160)
    precision: str = Field(default="bar", pattern=r"^(bar|trade|order_book)$")
    costs: dict[str, float] = Field(default_factory=dict)
    validation: str = Field(default="standard", pattern=r"^(quick|standard)$")
    seed: int = Field(default=7, ge=0)
    market: str = Field(default="spot", pattern=r"^(spot|perpetual)$")
    auxiliary_dataset_ids: list[str] = Field(default_factory=list, max_length=8)
    from_time: str | None = Field(default=None, alias="from")
    to_time: str | None = Field(default=None, alias="to")


class SourceResearchReportRequest(BaseModel):
    """Completed source-research evidence only; this endpoint never starts a legacy runtime."""

    model_config = ConfigDict(extra="forbid")
    evolution_results: list[dict[str, object]] = Field(max_length=500)
    walk_forward_results: list[dict[str, object]] = Field(max_length=500)
    catalog_results: list[dict[str, object]] = Field(max_length=500)


def create_workbench_app(settings: WorkbenchSettings) -> FastAPI:
    if bool(settings.origin_client_id) != bool(settings.origin_client_secret):
        raise ValueError("origin_client_id and origin_client_secret must be configured together")
    repository = ResearchRepository(
        settings.state_root / "research.sqlite3", queue_limit=settings.queue_limit
    )
    datasets = DatasetCatalog(settings.parquet_root, syncing_ids=repository.syncing_dataset_ids())
    auxiliary_datasets = AuxiliaryDatasetCatalog(settings.parquet_root)
    executor, native_runtime_error = _native_executor(settings)
    lab_service = LabService(
        LabRepository(settings.state_root / "research.sqlite3"),
        executor,
        timeline_provider=ParquetTimelineProvider(settings.parquet_root),
        research_repository=repository,
    )
    operations_service = OperationsService(
        research_repository=repository,
        state_root=settings.state_root,
        parquet_root=settings.parquet_root,
    )
    app = FastAPI(title="QT Research Workbench API", version="3.0.0")
    app.state.repository = repository
    app.state.lab_service = lab_service
    app.state.operations_service = operations_service
    app.state.native_runtime_error = native_runtime_error

    @app.middleware("http")
    async def require_authenticated_edge(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Require the Worker-injected service credentials when configured.

        Cloudflare Access headers from a browser are deliberately not trusted by
        the origin.  The canonical Worker validates the JWT then replaces them
        with these two origin-only credentials and an audited subject header.
        Local loopback development can omit the pair entirely.
        """

        if settings.origin_client_id and request.url.path.startswith("/api/"):
            supplied_id = request.headers.get("cf-access-client-id", "")
            supplied_secret = request.headers.get("cf-access-client-secret", "")
            subject = request.headers.get("x-qt-access-subject", "")
            valid = (
                hmac.compare_digest(supplied_id, settings.origin_client_id)
                and hmac.compare_digest(supplied_secret, settings.origin_client_secret or "")
                and bool(subject.strip())
            )
            if not valid:
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={
                        "detail": {
                            "code": "origin_auth_required",
                            "message": "request must pass through the authenticated edge",
                            "request_id": None,
                            "fields": {},
                        }
                    },
                )
        return await call_next(request)
    app.include_router(
        build_lab_router(LabSettings(settings.state_root), service=lab_service),
        prefix="/api/v3",
    )
    app.include_router(build_operations_router(operations_service), prefix="/api/v3")

    @app.get("/api/v3/provenance")
    def provenance_report(method: str | None = None) -> dict[str, object]:
        """Every threshold / band / constant with source, method and derivation.

        Front-ends must render a value together with ``method`` and
        ``sources`` (rule: no bare numbers)."""

        from qt.core import provenance as prov

        return {
            "params": prov.report(method),  # type: ignore[arg-type]
            "sources": {k: v.__dict__ for k, v in prov.SOURCES.items()},
            "unmapped_threshold_fields": prov.audit_threshold_config(),
        }

    @app.get("/api/v3/capabilities")
    def capabilities() -> dict[str, object]:
        return {
            "coverage": {"source_files": coverage_summary(), "semantic": capability_summary()},
            "items": manifest(),
            "semantic_items": capability_matrix(),
            "source_audit": source_audit(),
            "source_research_contract": RESEARCH_CONTRACT,
        }

    @app.get("/api/v3/source-research/contracts")
    def source_research_contracts() -> dict[str, object]:
        """Describe the retained pure source gates without claiming they are native strategies."""

        return {
            "items": [
                {"id": identifier, **contract}
                for identifier, contract in sorted(RESEARCH_CONTRACT.items())
            ],
            "next_cursor": None,
        }

    @app.post("/api/v3/source-research/reports")
    def source_research_report(request: SourceResearchReportRequest) -> dict[str, object]:
        """Build a source-faithful ranking/gate report from completed evidence."""

        try:
            return build_source_research_report(
                evolution_results=request.evolution_results,
                walk_forward_results=request.walk_forward_results,
                catalog_results=request.catalog_results,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise _api_error(422, "invalid_source_research_evidence", str(error)) from error

    @app.get("/api/v3/catalog")
    def catalog() -> dict[str, object]:
        source = legacy_catalog()
        return {
            "summary": catalog_summary(),
            "signals": source["signals"],
            "profiles": source["profiles"],
        }

    @app.get("/api/v3/indicators")
    def indicators() -> dict[str, object]:
        return {
            "items": [row for row in capability_matrix() if row["kind"] == "indicator_family"],
            "next_cursor": None,
        }

    @app.get("/api/v3/signals")
    def signals() -> dict[str, object]:
        return {"items": legacy_catalog()["signals"], "next_cursor": None}

    @app.get("/api/v3/auxiliary-datasets")
    def auxiliary_dataset_list() -> dict[str, object]:
        return {"items": auxiliary_datasets.list_datasets(), "next_cursor": None}

    @app.get("/api/v3/strategies/{profile_id}")
    def strategy(profile_id: str) -> dict[str, object]:
        try:
            return profile(profile_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown strategy profile") from error

    @app.post("/api/v3/experiments", status_code=status.HTTP_202_ACCEPTED)
    def submit_experiment(
        request: ExperimentRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        if not idempotency_key or not idempotency_key.strip():
            raise HTTPException(status_code=400, detail="Idempotency-Key is required")
        try:
            dataset = datasets.get(request.dataset_id)
            version = lab_service.execution_version(request.strategy_version_id)
            auxiliary_references = [
                auxiliary_datasets.reference(dataset_id)
                for dataset_id in request.auxiliary_dataset_ids
            ]
            normalized = _normalize_experiment_request(
                request,
                dataset,
                version,
                auxiliary_references,
            )
            existing = repository.lookup_idempotent(
                normalized,
                idempotency_key=idempotency_key.strip(),
            )
            if existing is not None:
                response.headers["Location"] = f"/api/v3/jobs/{existing['job_id']}"
                return {"job": _v3_job(existing), "created": False}
            _validate_precision_and_costs(request, dataset)
            if executor is None:
                raise _api_error(
                    503,
                    "native_runtime_unavailable",
                    native_runtime_error or "native research runtime is unavailable",
                )
            settings.resource_policy.admit(
                {"estimated_rows": dataset.get("rows", 0)},
                available_memory_mib=available_memory_mib(),
            )
            job, created = repository.enqueue_idempotent(
                normalized,
                idempotency_key=idempotency_key.strip(),
            )
        except CapacityBlockedError as error:
            raise _api_error(409, "capacity_blocked", str(error)) from error
        except IdempotencyConflictError as error:
            raise _api_error(409, "idempotency_conflict", str(error)) from error
        except (KeyError, ValueError) as error:
            raise _api_error(422, "invalid_experiment", str(error)) from error
        response.headers["Location"] = f"/api/v3/jobs/{job['job_id']}"
        return {"job": _v3_job(job), "created": created}

    @app.get("/api/v3/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, object]:
        try:
            return _v3_job(repository.get_job(job_id))
        except KeyError as error:
            raise _api_error(404, "unknown_job", "job was not found") from error

    @app.post("/api/v3/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, object]:
        try:
            return _v3_job(repository.request_cancel(job_id))
        except KeyError as error:
            raise _api_error(404, "unknown_job", "job was not found") from error

    @app.get("/api/v3/experiments")
    def experiments(limit: int = 50, cursor: int = 0) -> dict[str, object]:
        if not 1 <= limit <= 200 or cursor < 0:
            raise _api_error(422, "invalid_pagination", "limit must be 1..200 and cursor non-negative")
        items = repository.list_jobs(
            limit=limit,
            offset=cursor,
            job_types={"native_experiment"},
        )
        return {
            "items": [_v3_job(item) for item in items],
            "next_cursor": str(cursor + limit) if len(items) == limit else None,
        }

    @app.post("/api/v3/experiments/{job_id}/reproduce", status_code=status.HTTP_202_ACCEPTED)
    def reproduce_experiment(
        job_id: str,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        if not idempotency_key or not idempotency_key.strip():
            raise _api_error(400, "idempotency_required", "Idempotency-Key is required")
        try:
            original = repository.get_job(job_id)
            if original.get("job_type") != "native_experiment":
                raise ValueError("only a native experiment can be reproduced")
            original_spec = original.get("spec")
            if not isinstance(original_spec, Mapping):
                raise ValueError("experiment has no immutable request specification")
            reproduced = dict(original_spec)
            reproduced["reproduced_from"] = job_id
            existing = repository.lookup_idempotent(reproduced, idempotency_key=idempotency_key.strip())
            if existing is not None:
                response.headers["Location"] = f"/api/v3/jobs/{existing['job_id']}"
                return {"job": _v3_job(existing), "created": False}
            dataset_id = reproduced.get("dataset_id")
            if not isinstance(dataset_id, str):
                raise ValueError("experiment is missing dataset_id")
            dataset = datasets.get(dataset_id)
            if dataset.get("fingerprint") != reproduced.get("dataset_fingerprint"):
                raise ValueError("dataset fingerprint changed; reproduction requires the original immutable data")
            if executor is None:
                raise _api_error(503, "native_runtime_unavailable", native_runtime_error or "native runtime unavailable")
            settings.resource_policy.admit(
                {"estimated_rows": dataset.get("rows", 0)}, available_memory_mib=available_memory_mib()
            )
            job, created = repository.enqueue_idempotent(
                reproduced, idempotency_key=idempotency_key.strip()
            )
        except HTTPException:
            raise
        except CapacityBlockedError as error:
            raise _api_error(409, "capacity_blocked", str(error)) from error
        except IdempotencyConflictError as error:
            raise _api_error(409, "idempotency_conflict", str(error)) from error
        except KeyError as error:
            raise _api_error(404, "unknown_experiment", "experiment was not found") from error
        except ValueError as error:
            raise _api_error(422, "invalid_reproduction", str(error)) from error
        response.headers["Location"] = f"/api/v3/jobs/{job['job_id']}"
        return {"job": _v3_job(job), "created": created}

    @app.get("/api/v3/results")
    def results(limit: int = 50) -> dict[str, object]:
        if not 1 <= limit <= 200:
            raise _api_error(422, "invalid_limit", "limit must be between 1 and 200")
        return {"items": lab_service.list_results(limit=limit), "next_cursor": None}

    @app.get("/api/v3/artifacts")
    def artifacts(limit: int = 50) -> dict[str, object]:
        if not 1 <= limit <= 200:
            raise _api_error(422, "invalid_limit", "limit must be between 1 and 200")
        items: list[dict[str, object]] = []
        for result in lab_service.list_results(limit=limit):
            result_id, raw_artifacts = _result_artifacts(result)
            if not isinstance(result_id, str) or not isinstance(raw_artifacts, list):
                continue
            for artifact in raw_artifacts:
                if isinstance(artifact, Mapping) and isinstance(artifact.get("name"), str):
                    r2_key = artifact.get("r2_key")
                    download_url = _artifact_url(r2_key, result_id=result_id, name=artifact["name"])
                    published = artifact.get("publication_status") == "published" and download_url is not None
                    items.append(
                        {
                            "artifact_id": f"{result_id}:{artifact['name']}",
                            "result_id": result_id,
                            "name": artifact["name"],
                            "status": "published" if published else artifact.get("publication_status", "unpublished"),
                            "published": published,
                            "download_url": download_url if published else None,
                        }
                    )
        return {"items": items[:limit], "next_cursor": None}

    @app.get("/api/v3/artifacts/{artifact_id}")
    def artifact(artifact_id: str) -> dict[str, object]:
        result_id, separator, name = artifact_id.partition(":")
        if not separator or not result_id or not name:
            raise _api_error(404, "unknown_artifact", "artifact was not found")
        try:
            result = lab_service.repository.get_result(result_id)
        except KeyError as error:
            raise _api_error(404, "unknown_artifact", "artifact was not found") from error
        _, raw_artifacts = _result_artifacts(result)
        if not isinstance(raw_artifacts, list):
            raise _api_error(404, "unknown_artifact", "artifact was not found")
        match = next(
            (item for item in raw_artifacts if isinstance(item, Mapping) and item.get("name") == name),
            None,
        )
        if match is None:
            raise _api_error(404, "unknown_artifact", "artifact was not found")
        r2_key = match.get("r2_key")
        download_url = _artifact_url(r2_key, result_id=result_id, name=name)
        published = match.get("publication_status") == "published" and download_url is not None
        return {
            "artifact_id": artifact_id,
            "result_id": result_id,
            "name": name,
            "status": "published" if published else match.get("publication_status", "unpublished"),
            "published": published,
            "download_url": download_url if published else None,
        }

    return app


def _normalize_experiment_request(
    request: ExperimentRequest,
    dataset: dict[str, object],
    strategy_version: dict[str, object],
    auxiliary_references: list[AuxiliaryReference],
) -> dict[str, object]:
    if dataset.get("status") != "ready":
        raise ValueError(f"dataset is not ready: {request.dataset_id}")
    declared_market = dataset.get("market")
    if not isinstance(declared_market, str) or declared_market not in {"spot", "perpetual"}:
        raise ValueError("dataset has no declared spot/perpetual market contract")
    if request.market != declared_market:
        raise ValueError(
            f"requested market {request.market} does not match dataset market {declared_market}"
        )
    if declared_market == "perpetual":
        raise ValueError(
            "perpetual and leveraged research are disabled pending independent "
            "quote/mark, margin, and liquidation acceptance",
        )
    symbol = dataset.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("dataset has no declared symbol contract")
    validate_auxiliary_selection(
        auxiliary_references,
        primary_symbol=symbol,
        primary_market=declared_market,
        required_kinds=_required_auxiliary_kinds(strategy_version, declared_market),
    )
    known_costs = {
        "initial_cash",
        "fee_bps",
        "maker_fee_bps",
        "taker_fee_bps",
        "spread_bps",
        "slippage_bps",
        "funding_bps",
        "borrow_bps",
        "leverage",
        "maintenance_margin",
        "liquidation_trigger_ratio",
    }
    unknown_costs = set(request.costs).difference(known_costs)
    if unknown_costs:
        raise ValueError(f"unknown cost assumptions: {', '.join(sorted(unknown_costs))}")
    if any(not isfinite(value) or value < 0 for value in request.costs.values()):
        raise ValueError("cost assumptions must be finite and non-negative")
    if request.costs.get("initial_cash", 10_000) <= 0:
        raise ValueError("initial_cash must be positive")
    leverage = request.costs.get("leverage", 1)
    if not 1 <= leverage <= 3:
        raise ValueError("research leverage must be between 1x and 3x")
    _validate_window(request.from_time, request.to_time)
    return {
        "job_type": "native_experiment",
        "dataset_id": request.dataset_id,
        "ohlcv_key": dataset["key"],
        "dataset_fingerprint": dataset["fingerprint"],
        "strategy_version_id": request.strategy_version_id,
        "lab_strategy_version": strategy_version,
        "mode": "lab_strategy_version",
        "precision": request.precision,
        "assumptions": dict(request.costs),
        "validation_profile": request.validation,
        "seed": request.seed,
        "market": declared_market,
        "auxiliary_datasets": [reference.as_spec() for reference in auxiliary_references],
        "from": request.from_time,
        "to": request.to_time,
    }


def _required_auxiliary_kinds(
    strategy_version: Mapping[str, object],
    market: str,
) -> frozenset[str]:
    """Resolve only the source requirements needed before a durable enqueue."""

    required: set[str] = {"mark_quotes"} if market == "perpetual" else set()
    content = strategy_version.get("content")
    identity = content.get("builtin_identity") if isinstance(content, Mapping) else None
    if identity == "btcqt:btcqt_s2_crowding_fader":
        required.update({"funding_settlements", "open_interest"})
    return frozenset(required)


def _validate_precision_and_costs(request: ExperimentRequest, dataset: Mapping[str, object]) -> None:
    raw_precisions = dataset.get("precisions", ["bar"])
    precisions = raw_precisions if isinstance(raw_precisions, list) else []
    if request.precision not in precisions:
        raise ValueError(
            f"dataset does not provide {request.precision} precision; available: "
            + ", ".join(str(value) for value in precisions if isinstance(value, str))
        )
    # The current native bar path has observable fees and configured one-tick
    # slippage only.  It must reject costs whose feed/model is absent rather
    # than accept and ignore them.
    if request.precision == "bar":
        unavailable = [
            name
            for name in ("spread_bps", "borrow_bps", "funding_bps")
            if request.costs.get(name, 0) != 0
        ]
        if unavailable:
            raise ValueError(
                "bar precision cannot model " + ", ".join(unavailable) + "; use a dataset/model that declares it"
            )


def _result_artifacts(result: Mapping[str, object]) -> tuple[str | None, list[object]]:
    """Read Lab's immutable result envelope without leaking local artifact paths."""

    result_id = result.get("result_id", result.get("run_id"))
    summary = result.get("summary", result)
    if not isinstance(summary, Mapping):
        return None, []
    raw_artifacts = summary.get("artifacts", [])
    return (result_id if isinstance(result_id, str) else None, raw_artifacts if isinstance(raw_artifacts, list) else [])


def _artifact_url(r2_key: object, *, result_id: str, name: str) -> str | None:
    if not _safe_artifact_component(result_id) or not _safe_artifact_component(name):
        return None
    expected = f"reports/{result_id}/{name}"
    if not isinstance(r2_key, str) or not hmac.compare_digest(r2_key, expected):
        return None
    return f"/artifacts/{result_id}/{name}"


def _safe_artifact_component(value: str) -> bool:
    return bool(value) and all(
        character in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in value
    )


def _native_executor(settings: WorkbenchSettings) -> tuple[LabExecutionAdapter | None, str | None]:
    """Load the optional Python 3.12 native executor without breaking API inspection."""

    try:
        from qt.nautilus.executor import NautilusResearchExecutor

        return (
            NautilusResearchExecutor(
                settings.parquet_root,
                settings.artifact_root or settings.state_root / "artifacts",
            ),
            None,
        )
    except (ImportError, RuntimeError) as error:
        return None, f"native research runtime unavailable: {error}"


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "request_id": None, "fields": {}},
    )


def _v3_job(job: dict[str, object]) -> dict[str, object]:
    """Present v3 lifecycle names without changing the legacy v2 repository API."""

    response = dict(job)
    legacy_status = response.get("status")
    response["status"] = response.get("v3_status", legacy_status)
    response["legacy_status"] = legacy_status
    response.pop("v3_status", None)
    return response


def _validate_window(start: str | None, end: str | None) -> None:
    if start is None and end is None:
        return
    if start is None or end is None:
        raise ValueError("from and to must be supplied together")
    try:
        start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("from and to must be ISO-8601 timestamps") from error
    if start_at.tzinfo is None or end_at.tzinfo is None or start_at >= end_at:
        raise ValueError("from and to must be aware timestamps with from before to")
