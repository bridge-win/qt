"""Conflict-safe FastAPI router for the lab service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Response, status

from qt.lab.persistence import LabConflictError, LabRepository, TraceTimestampUnavailableError
from qt.lab.schemas import (
    CompareRequest,
    CreateStrategyRequest,
    CreateVersionRequest,
    NoteCreateRequest,
    NoteUpdateRequest,
    OptimizationRequest,
    ValidateStrategyRequest,
    ValidationRequest,
)
from qt.lab.service import LabExecutionAdapter, LabService, ParquetTimelineProvider
from qt.research.repository import IdempotencyConflictError, ResearchRepository


@dataclass(frozen=True)
class LabSettings:
    state_root: Path
    parquet_root: Path | None = None


def build_lab_router(
    settings: LabSettings,
    *,
    executor: LabExecutionAdapter | None = None,
    repository: LabRepository | None = None,
    research_repository: ResearchRepository | None = None,
    service: LabService | None = None,
) -> APIRouter:
    """Return routes mounted by the lead under ``/api/v3``.

    Do not add ``GET /strategies/{id}``: that exact method/path belongs to the
    workbench profile catalog. Lab strategy detail is supplied by immutable
    version routes, avoiding ambiguous route precedence.
    """

    if service is not None and (
        executor is not None or repository is not None or research_repository is not None
    ):
        raise ValueError("pass either service or its repository/executor dependencies")
    lab_repository = repository or LabRepository(settings.state_root / "research.sqlite3")
    service = service or LabService(
        lab_repository,
        executor,
        timeline_provider=(
            ParquetTimelineProvider(settings.parquet_root) if settings.parquet_root else None
        ),
        research_repository=research_repository
        or ResearchRepository(settings.state_root / "research.sqlite3"),
    )
    router = APIRouter()

    @router.get("/strategies")
    def strategies() -> dict[str, object]:
        builtins = service.catalog.list_builtins()
        drafts = service.repository.list_strategies()
        return {"items": [*builtins, *drafts], "next_cursor": None}

    @router.post("/strategies", status_code=status.HTTP_201_CREATED)
    def create_strategy(request: CreateStrategyRequest) -> dict[str, object]:
        try:
            return service.create_strategy(request)
        except ValueError as error:
            raise _error(422, "invalid_strategy", str(error)) from error

    @router.post("/strategies/validate")
    def validate_strategy(request: ValidateStrategyRequest) -> dict[str, object]:
        try:
            return service.validate(request)
        except KeyError as error:
            raise _error(422, "unknown_strategy_version", str(error)) from error
        except ValueError as error:
            raise _error(422, "invalid_strategy", str(error)) from error

    @router.get("/strategies/{strategy_id}/versions")
    def strategy_versions(strategy_id: str) -> dict[str, object]:
        try:
            return {"items": service.repository.list_versions(strategy_id), "next_cursor": None}
        except KeyError as error:
            raise _error(404, "unknown_strategy", "strategy draft was not found") from error

    @router.post("/strategies/{strategy_id}/versions", status_code=status.HTTP_201_CREATED)
    def create_version(strategy_id: str, request: CreateVersionRequest) -> dict[str, object]:
        try:
            return service.create_version(strategy_id, request)
        except LabConflictError as error:
            current = service.repository.get_strategy(strategy_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "version_conflict",
                    "message": str(error),
                    "current_version": current["revision"],
                    "current": current,
                },
            ) from error
        except KeyError as error:
            raise _error(
                404, "unknown_strategy", "strategy draft or linked version was not found"
            ) from error
        except ValueError as error:
            raise _error(422, "invalid_strategy", str(error)) from error

    @router.get("/strategy-versions/{version_id}")
    def strategy_version(version_id: str) -> dict[str, object]:
        try:
            return service.repository.get_version(version_id)
        except KeyError as error:
            raise _error(
                404, "unknown_strategy_version", "immutable strategy version was not found"
            ) from error

    @router.get("/strategy-docs/{strategy_id}")
    def strategy_docs(strategy_id: str) -> dict[str, object]:
        try:
            return service.strategy_documentation(strategy_id)
        except KeyError as error:
            raise _error(404, "unknown_strategy", "strategy documentation was not found") from error

    @router.get("/indicator-docs/{indicator_id}")
    def indicator_docs(indicator_id: str) -> dict[str, object]:
        try:
            return service.indicator_documentation(indicator_id)
        except KeyError as error:
            raise _error(
                404, "unknown_indicator", "indicator documentation was not found"
            ) from error

    @router.get("/learning-docs")
    def learning_docs() -> dict[str, object]:
        return {"items": service.learning_documentation(), "next_cursor": None}

    @router.get("/results/{result_id}")
    def result(result_id: str) -> dict[str, object]:
        try:
            return service.repository.get_result(result_id)
        except KeyError as error:
            raise _error(404, "unknown_result", "result was not found") from error

    @router.get("/results/{result_id}/traces")
    def result_traces(
        result_id: str,
        cursor: int | None = None,
        window: int = 100,
        from_time: Annotated[str | None, Query(alias="from")] = None,
        to_time: Annotated[str | None, Query(alias="to")] = None,
    ) -> dict[str, object]:
        if not 1 <= window <= 200:
            raise _error(422, "invalid_window", "window must be between 1 and 200")
        if cursor is not None and cursor < 0:
            raise _error(422, "invalid_cursor", "cursor must be non-negative")
        start, end = _trace_window(from_time, to_time)
        try:
            service.repository.get_result(result_id)
            return service.repository.traces(
                result_id, cursor, window, from_time=start, to_time=end
            )
        except KeyError as error:
            raise _error(404, "unknown_result", "result was not found") from error
        except TraceTimestampUnavailableError as error:
            raise _error(422, "trace_timestamps_unavailable", str(error)) from error

    @router.post("/compare")
    def compare(request: CompareRequest) -> dict[str, object]:
        try:
            return service.compare(request)
        except KeyError as error:
            raise _error(404, "unknown_result", "one or more results were not found") from error

    @router.get("/notes")
    def notes(strategy_version_id: str | None = None) -> dict[str, object]:
        return {"items": service.repository.list_notes(strategy_version_id), "next_cursor": None}

    @router.post("/notes", status_code=status.HTTP_201_CREATED)
    def create_note(request: NoteCreateRequest) -> dict[str, object]:
        try:
            return service.create_note(request)
        except KeyError as error:
            raise _error(
                422, "unknown_strategy_version", "notes must bind to an immutable strategy version"
            ) from error

    @router.get("/notes/{note_id}")
    def note(note_id: str) -> dict[str, object]:
        try:
            return service.repository.get_note(note_id)
        except KeyError as error:
            raise _error(404, "unknown_note", "note was not found") from error

    @router.patch("/notes/{note_id}")
    def update_note(note_id: str, request: NoteUpdateRequest) -> dict[str, object]:
        try:
            return service.repository.update_note(note_id, request.body)
        except KeyError as error:
            raise _error(404, "unknown_note", "note was not found") from error

    @router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_note(note_id: str) -> Response:
        try:
            service.repository.delete_note(note_id)
        except KeyError as error:
            raise _error(404, "unknown_note", "note was not found") from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/optimizations", status_code=status.HTTP_202_ACCEPTED)
    def optimization(
        request: OptimizationRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        _require_idempotency(idempotency_key)
        assert idempotency_key is not None
        try:
            job, created = service.enqueue_optimization(
                request, idempotency_key=idempotency_key.strip()
            )
        except IdempotencyConflictError as error:
            raise _error(409, "idempotency_conflict", str(error)) from error
        except KeyError as error:
            raise _error(
                422,
                "unknown_strategy_version",
                "optimization requires an immutable strategy version",
            ) from error
        except ValueError as error:
            raise _error(422, "invalid_optimization", str(error)) from error
        response.headers["Location"] = f"/api/v3/optimizations/{job['job_id']}"
        return {"job": job, "created": created}

    @router.get("/optimizations/{job_id}")
    def get_optimization(job_id: str) -> dict[str, object]:
        try:
            return service.get_optimization(job_id)
        except KeyError as error:
            raise _error(404, "unknown_optimization", "optimization was not found") from error

    @router.post("/validation", status_code=status.HTTP_202_ACCEPTED)
    def validation(
        request: ValidationRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        _require_idempotency(idempotency_key)
        assert idempotency_key is not None
        try:
            job, created = service.enqueue_validation(
                request, idempotency_key=idempotency_key.strip()
            )
        except IdempotencyConflictError as error:
            raise _error(409, "idempotency_conflict", str(error)) from error
        except KeyError as error:
            raise _error(
                422, "unknown_strategy_version", "validation requires an immutable strategy version"
            ) from error
        except ValueError as error:
            raise _error(422, "invalid_validation", str(error)) from error
        response.headers["Location"] = f"/api/v3/validation/{job['job_id']}"
        return {"job": job, "created": created}

    @router.get("/traces")
    def traces(
        experiment_id: str,
        cursor: int | None = None,
        limit: int = 100,
        from_time: Annotated[str | None, Query(alias="from")] = None,
        to_time: Annotated[str | None, Query(alias="to")] = None,
    ) -> dict[str, object]:
        if not 1 <= limit <= 200:
            raise _error(422, "invalid_limit", "limit must be between 1 and 200")
        if cursor is not None and cursor < 0:
            raise _error(422, "invalid_cursor", "cursor must be non-negative")
        start, end = _trace_window(from_time, to_time)
        try:
            service.repository.get_result(experiment_id)
            return service.repository.traces(
                experiment_id, cursor, limit, from_time=start, to_time=end
            )
        except KeyError as error:
            raise _error(404, "unknown_result", "result was not found") from error
        except TraceTimestampUnavailableError as error:
            raise _error(422, "trace_timestamps_unavailable", str(error)) from error

    @router.get("/validation/{job_id}")
    def get_validation(job_id: str) -> dict[str, object]:
        try:
            return service.get_validation(job_id)
        except KeyError as error:
            raise _error(404, "unknown_validation", "validation was not found") from error

    return router


def _require_idempotency(value: str | None) -> None:
    if value is None or not value.strip():
        raise _error(400, "idempotency_required", "Idempotency-Key is required")


def _trace_window(from_time: str | None, to_time: str | None) -> tuple[str | None, str | None]:
    if (from_time is None) != (to_time is None):
        raise _error(422, "invalid_trace_window", "from_time and to_time must be supplied together")
    if from_time is None or to_time is None:
        return None, None
    try:
        start = datetime.fromisoformat(from_time.replace("Z", "+00:00"))
        end = datetime.fromisoformat(to_time.replace("Z", "+00:00"))
    except ValueError as error:
        raise _error(422, "invalid_trace_window", "trace times must be ISO-8601") from error
    if start.tzinfo is None or end.tzinfo is None:
        raise _error(422, "invalid_trace_window", "trace times must be timezone-aware")
    if start >= end:
        raise _error(422, "invalid_trace_window", "from_time must be earlier than to_time")
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


def _error(code: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"code": error_code, "message": message, "request_id": None, "fields": {}},
    )
