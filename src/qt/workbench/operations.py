"""Bounded, research-only operations API for the QT workbench.

The router intentionally has no application-level prefix.  The owning API
module includes it once under ``/api/v3`` after removing its older duplicate
dataset/source/runtime endpoints.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal, TypeAlias, TypeVar, cast

import pandas as pd
import pyarrow.parquet as pq
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from qt.data.catalog import data_source_statuses
from qt.data.store import ParquetStore
from qt.intel.ranker import read_opportunities
from qt.legacy.btc_quant_evolution.external_data.providers import all_providers
from qt.legacy.btc_quant_evolution.external_data.schemas import (
    ExternalDataProvider,
    NormalizedRow,
    ProviderPlanItem,
    ProviderSyncRequest,
)
from qt.portfolio.reader import read_all_portfolios
from qt.research.datasets import DatasetCatalog
from qt.research.repository import IdempotencyConflictError as ResearchIdempotencyConflictError
from qt.research.repository import ResearchRepository
from qt.workbench.providers import provider_catalog
from qt.workbench.resources import available_memory_mib

JsonDict: TypeAlias = dict[str, object]
Progress: TypeAlias = Callable[[str, int], None]
Cancelled: TypeAlias = Callable[[], bool]
RequestModel = TypeVar("RequestModel", bound=BaseModel)

MAX_LIST_LIMIT = 200
MAX_IMPORT_BYTES = 64 * 1024 * 1024
MAX_BROWSER_UPLOAD_BYTES = 1 * 1024 * 1024
MAX_IMPORT_ROWS = 200_000
MAX_WINDOW_DAYS = 366
MAX_SYNC_PLAN_ITEMS = 32
_IDEMPOTENCY_MAX_LENGTH = 256
_RUNTIME_COMMANDS = {"paper.pause", "paper.resume"}
_OPAQUE_OBJECT_KEY = re.compile(r"uploads/[a-f0-9]{32}\.(?:csv|parquet)$")


class OperationsError(ValueError):
    """A client-visible operation validation error."""


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["staging"]
    dataset_id: str = Field(min_length=1, max_length=120)
    format: Literal["csv", "parquet"]
    object_key: str = Field(min_length=1, max_length=512)


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str = Field(min_length=1, max_length=80)
    dataset_id: str = Field(min_length=1, max_length=80)
    symbol: str = Field(default="BTCUSDT", min_length=2, max_length=30)
    timeframe: str = Field(default="1d", pattern=r"^(?:1m|5m|15m|1h|4h|8h|1d)$")
    from_time: datetime = Field(alias="from")
    to_time: datetime = Field(alias="to")


class RuntimeCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=80)
    reason: str = Field(default="", max_length=500)


class OperationsStore:
    """Operations metadata only: immutable audit rows, never another job queue."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def record_audit(
        self,
        *,
        command: str,
        before: Mapping[str, object],
        after: Mapping[str, object],
        reason: str,
    ) -> JsonDict:
        entry: JsonDict = {
            "audit_id": uuid.uuid4().hex,
            "at": _now(),
            "command": command,
            "reason": reason,
            "before": dict(before),
            "after": dict(after),
        }
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO operation_audit
                    (audit_id, at, command, reason, before_json, after_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entry["audit_id"], entry["at"], command, reason,
                    _canonical_json(before), _canonical_json(after),
                ),
            )
        return entry

    def list_audit(self, *, cursor: str | None, limit: int) -> tuple[list[JsonDict], str | None]:
        offset = _parse_cursor(cursor)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT audit_id, at, command, reason, before_json, after_json
                FROM operation_audit ORDER BY at DESC, audit_id DESC LIMIT ? OFFSET ?
                """,
                (limit + 1, offset),
            ).fetchall()
        selected = rows[:limit]
        items: list[JsonDict] = [
            {
                "audit_id": str(row["audit_id"]),
                "at": str(row["at"]),
                "command": str(row["command"]),
                "reason": str(row["reason"]),
                "before": _json_object(str(row["before_json"])),
                "after": _json_object(str(row["after_json"])),
            }
            for row in selected
        ]
        return items, str(offset + limit) if len(rows) > limit else None

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operation_audit (
                    audit_id TEXT PRIMARY KEY,
                    at TEXT NOT NULL,
                    command TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_operation_audit_at ON operation_audit(at DESC);
                """
            )

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

class OperationsService:
    """Adapter around real repositories, parquet data, and paper-state protocols."""

    def __init__(
        self,
        *,
        research_repository: ResearchRepository,
        state_root: Path,
        parquet_root: Path,
        staging_root: Path | None = None,
        operations_store: OperationsStore | None = None,
        providers: Sequence[ExternalDataProvider] | None = None,
        paper_control_path: Path | None = None,
    ) -> None:
        self.research_repository = research_repository
        self.state_root = state_root
        self.parquet_root = parquet_root
        self.staging_root = staging_root or state_root / "staging"
        self.store = operations_store or OperationsStore(state_root / "operations.sqlite3")
        self.providers = {provider.name: provider for provider in (providers or all_providers())}
        self.paper_control_path = paper_control_path or state_root / "control_paper.json"

    def datasets(self) -> list[JsonDict]:
        catalog = DatasetCatalog(self.parquet_root, syncing_ids=self.research_repository.syncing_dataset_ids())
        return catalog.list_datasets()

    def sources(self) -> list[JsonDict]:
        local = data_source_statuses(ParquetStore(self.parquet_root))
        rows: list[JsonDict] = []
        for provider in provider_catalog():
            item = dict(provider)
            item["kind"] = "provider"
            item["available"] = bool(item["selected"])
            item["availability_note"] = "configured adapter; freshness and data validity are reported separately"
            rows.append(item)
        rows.extend({"kind": "local_source", **row} for row in local)
        return rows

    def submit_import(self, request: ImportRequest, idempotency_key: str) -> tuple[JsonDict, bool]:
        payload = request.model_dump()
        if _OPAQUE_OBJECT_KEY.fullmatch(str(payload["object_key"])) is None:
            raise OperationsError("object_key must be an opaque key returned by /staging/uploads")
        path = _contained_file(self.staging_root, str(payload["object_key"]))
        if not path.exists() or not path.is_file():
            raise OperationsError("staged object does not exist")
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise OperationsError("staged object exceeds the 64 MiB import limit")
        return self._enqueue_shared("data_import", payload, idempotency_key)

    async def stage_upload(self, request: Request) -> JsonDict:
        """Accept the edge-safe browser upload and return an opaque staging key."""

        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > MAX_BROWSER_UPLOAD_BYTES:
                    raise OperationsError("browser upload exceeds the 1 MiB edge-safe limit")
            except ValueError as error:
                raise OperationsError("Content-Length must be numeric") from error
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].lower()
        suffix = _upload_suffix(content_type, request.headers.get("x-upload-format"))
        object_key = f"uploads/{uuid.uuid4().hex}{suffix}"
        target = _contained_file(self.staging_root, object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_BROWSER_UPLOAD_BYTES:
                        raise OperationsError("browser upload exceeds the 1 MiB edge-safe limit")
                    handle.write(chunk)
                if size == 0:
                    raise OperationsError("browser upload body is empty")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
        except BaseException:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        return {
            "object_key": object_key,
            "format": "csv" if suffix == ".csv" else "parquet",
            "size_bytes": size,
            "max_size_bytes": MAX_BROWSER_UPLOAD_BYTES,
        }

    def submit_sync(self, request: SyncRequest, idempotency_key: str) -> tuple[JsonDict, bool]:
        payload = request.model_dump(by_alias=True, mode="json")
        _require_aware(request.from_time)
        _require_aware(request.to_time)
        if request.to_time <= request.from_time:
            raise OperationsError("to must be after from")
        if request.to_time - request.from_time > timedelta(days=MAX_WINDOW_DAYS):
            raise OperationsError(f"sync window may not exceed {MAX_WINDOW_DAYS} days")
        provider = self.providers.get(request.source)
        if provider is None:
            raise OperationsError("source is not an allow-listed provider")
        capability = next((item for item in provider.capabilities() if item.dataset == request.dataset_id), None)
        if capability is None or not capability.implemented:
            raise OperationsError("provider cannot sync this dataset")
        payload["symbol"] = _canonical_symbol(request.symbol)
        return self._enqueue_shared("data_sync", payload, idempotency_key)

    def execute(
        self,
        spec: Mapping[str, object],
        progress: Progress,
        cancelled: Cancelled,
    ) -> JsonDict:
        """Execute one shared-repository operation spec from the common worker.

        The common ``ResearchWorker`` remains responsible for claim, lease,
        heartbeat, cancellation, recovery, and completion fencing.  This method
        only dispatches an already-claimed operation and returns its result.
        """

        job_type = _required_string(spec, "job_type")
        payload = spec.get("payload")
        if not isinstance(payload, Mapping):
            raise OperationsError("operation payload must be an object")
        if cancelled():
            raise OperationsError("operation was cancelled before execution")
        if job_type == "data_import":
            return dict(self._handle_import(payload, progress, cancelled))
        if job_type == "data_sync":
            return dict(self._handle_sync(payload, progress, cancelled))
        raise OperationsError("unknown shared operation job_type")

    def events_wicks(self, *, dataset_id: str, from_time: datetime, to_time: datetime, limit: int) -> list[JsonDict]:
        frame = self._read_dataset_window(dataset_id, from_time, to_time)
        required = {"open", "high", "low", "close"}
        if not required.issubset(frame.columns):
            raise OperationsError("dataset has no OHLC wick fields")
        span = (frame["high"] - frame["low"]).replace(0, pd.NA)
        lower = ((frame[["open", "close"]].min(axis=1) - frame["low"]) / span).fillna(0.0)
        body = (frame["close"] - frame["open"]).abs() / span
        hits = frame.loc[(lower >= 0.45) & (lower > body)].copy()
        hits["lower_wick_ratio"] = lower.loc[hits.index]
        records: list[JsonDict] = []
        for timestamp, row in hits.sort_index(ascending=False).head(limit).iterrows():
            records.append(
                {
                    "timestamp": _timestamp_string(timestamp),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "lower_wick_ratio": round(float(row["lower_wick_ratio"]), 6),
                    "explanation": "lower wick exceeds 45% of the candle range and the candle body",
                }
            )
        return records

    def opportunities(self, *, cursor: str | None, limit: int, as_of: datetime | None) -> tuple[list[JsonDict], str | None, str | None, str]:
        payload = read_opportunities(self.state_root / "runtime")
        generated_at = payload.get("generated_at")
        generated_text = generated_at if isinstance(generated_at, str) else None
        generated_at_status = "missing"
        generated_time: datetime | None = None
        if generated_text is not None:
            try:
                generated_time = _parse_aware_time(generated_text, "persisted generated_at")
                generated_at_status = "valid"
            except OperationsError:
                generated_at_status = "invalid"
        if as_of is not None:
            if generated_at_status != "valid":
                return [], None, generated_text, generated_at_status
            if generated_time is not None and generated_time > _as_utc(as_of):
                return [], None, generated_text, generated_at_status
        values = payload.get("opportunities")
        items = [dict(item) for item in values if isinstance(item, Mapping)] if isinstance(values, list) else []
        offset = _parse_cursor(cursor)
        selected = items[offset : offset + limit]
        for item in selected:
            item["explanation"] = str(item.get("why") or "scanner persisted no explanatory reason")
        next_cursor = str(offset + limit) if offset + limit < len(items) else None
        return selected, next_cursor, generated_text, generated_at_status

    def portfolio(self, *, cursor: str | None, limit: int) -> tuple[list[JsonDict], str | None]:
        snapshots = read_all_portfolios(self.state_root / "runtime")
        offset = _parse_cursor(cursor)
        selected: list[JsonDict] = []
        for snapshot in snapshots[offset : offset + limit]:
            item = {key: value for key, value in snapshot.items() if key != "trades"}
            item["explanation"] = "Durable paper ledger summary; detailed fills remain in the local ledger."
            selected.append(cast(JsonDict, item))
        next_cursor = str(offset + limit) if offset + limit < len(snapshots) else None
        return selected, next_cursor

    def runtime(self) -> JsonDict:
        paper = self._paper_state(read_only=True)
        return {
            "mode": "research",
            "live_enabled": False,
            "available_memory_mib": available_memory_mib(),
            "worker": self.research_repository.worker_status(online_within=timedelta(minutes=2)),
            "paper": paper,
        }

    def runtime_command(self, request: RuntimeCommandRequest) -> JsonDict:
        if request.command not in _RUNTIME_COMMANDS:
            raise OperationsError("runtime command is unavailable; live controls are never exposed")
        before = self.runtime()
        self._write_paper_pause(request.command == "paper.pause")
        after = self.runtime()
        audit = self.store.record_audit(
            command=request.command, before=before, after=after, reason=request.reason
        )
        return {"runtime": after, "audit": audit}

    def _enqueue_shared(
        self,
        job_type: Literal["data_import", "data_sync"],
        payload: Mapping[str, object],
        idempotency_key: str,
    ) -> tuple[JsonDict, bool]:
        key = _validate_idempotency_key(idempotency_key)
        try:
            return self.research_repository.enqueue_idempotent(
                {"job_type": job_type, "payload": dict(payload)}, idempotency_key=key
            )
        except ResearchIdempotencyConflictError as error:
            raise OperationsError("idempotency key was already used with a different request") from error
        except ValueError as error:
            if "queue is full" in str(error):
                raise OperationsError("shared research queue is full on this node") from error
            raise OperationsError(str(error)) from error

    def _handle_import(
        self, payload: Mapping[str, object], progress: Progress, cancelled: Cancelled
    ) -> Mapping[str, object]:
        progress("import_validate", 20)
        if cancelled():
            raise OperationsError("operation cancelled before import")
        format_name = _required_string(payload, "format")
        dataset_id = _required_string(payload, "dataset_id")
        source_path = _contained_file(self.staging_root, _required_string(payload, "object_key"))
        if source_path.stat().st_size > MAX_IMPORT_BYTES:
            raise OperationsError("staged object exceeds the 64 MiB import limit")
        frame = _read_staged_frame(source_path, format_name)
        frame = _validated_ohlcv_frame(frame)
        fingerprint = _frame_fingerprint(frame)
        target = self.parquet_root / "ohlcv" / f"{_safe_dataset_key(dataset_id)}.parquet"
        progress("import_publish", 85)
        if cancelled():
            raise OperationsError("operation cancelled before publish")
        _publish_parquet_atomic(frame, target)
        return {
            "dataset_id": dataset_id,
            "path": target.relative_to(self.parquet_root).as_posix(),
            "rows": len(frame),
            "fingerprint": fingerprint,
            "quality": {"accepted": True, "required_columns": ["open", "high", "low", "close", "volume"]},
        }

    def _handle_sync(
        self, payload: Mapping[str, object], progress: Progress, cancelled: Cancelled
    ) -> Mapping[str, object]:
        progress("sync_plan", 15)
        if cancelled():
            raise OperationsError("operation cancelled before provider sync")
        source = _required_string(payload, "source")
        dataset_id = _required_string(payload, "dataset_id")
        provider = self.providers.get(source)
        if provider is None:
            raise OperationsError("source is not an allow-listed provider")
        from_time = _parse_aware_time(_required_string(payload, "from"), "from")
        to_time = _parse_aware_time(_required_string(payload, "to"), "to")
        if to_time <= from_time:
            raise OperationsError("to must be after from")
        symbol = _canonical_symbol(_required_string(payload, "symbol"))
        end_date = to_time.date() if to_time.time() == datetime.min.time() else (to_time + timedelta(days=1)).date()
        request = ProviderSyncRequest(
            symbol=symbol,
            timeframe=_required_string(payload, "timeframe"),
            start=from_time.date(),
            end=end_date,
            source_policy="free-only",
        )
        capability = next((item for item in provider.capabilities() if item.dataset == dataset_id), None)
        if capability is None or not capability.implemented:
            raise OperationsError("provider cannot sync this dataset")
        if provider.credential_status(os.environ) != "enabled":
            raise OperationsError("provider credentials are not available")
        plan = [item for item in provider.plan_sync(request) if item.dataset == dataset_id]
        if not plan:
            raise OperationsError("provider produced no sync plan for the requested dataset")
        if len(plan) > MAX_SYNC_PLAN_ITEMS:
            raise OperationsError(f"provider sync plan exceeds the {MAX_SYNC_PLAN_ITEMS} item limit")
        _validate_sync_plan(plan, source=source, symbol=symbol, from_time=from_time, to_time=to_time)
        progress("sync_fetch", 45)
        rows: list[NormalizedRow] = []
        for position, item in enumerate(plan, start=1):
            if cancelled():
                raise OperationsError("operation cancelled before normalization")
            records = provider.fetch(item)
            normalized = provider.normalize(item, records)
            rows.extend(normalized)
            if len(rows) > MAX_IMPORT_ROWS:
                raise OperationsError("provider result exceeds the one million row limit")
            progress("sync_fetch", 45 + int(position * 25 / len(plan)))
        progress("sync_validate", 75)
        frame = _normalized_rows_frame(rows, required_fields=capability.required_fields)
        frame = frame.loc[(frame.index >= from_time) & (frame.index <= to_time)]
        if frame.empty:
            raise OperationsError("provider returned no records inside the requested window")
        key = f"{source}_{_symbol_path_key(symbol)}_{request.timeframe}"
        target = self.parquet_root / dataset_id / f"{key}.parquet"
        progress("sync_publish", 90)
        if cancelled():
            raise OperationsError("operation cancelled before publish")
        _publish_parquet_atomic(frame, target)
        return {
            "source": source,
            "dataset_id": dataset_id,
            "path": target.relative_to(self.parquet_root).as_posix(),
            "rows": len(frame),
            "fingerprint": _frame_fingerprint(frame),
            "quality": {
                "accepted": True,
                "available_at_checked": True,
                "plan_items_executed": len(plan),
                "requested_window": {"from": from_time.isoformat(), "to": to_time.isoformat()},
                "coverage_status": _coverage_status(frame, from_time=from_time, to_time=to_time),
            },
        }

    def _read_dataset_window(self, dataset_id: str, from_time: datetime, to_time: datetime) -> pd.DataFrame:
        if to_time <= from_time or to_time - from_time > timedelta(days=MAX_WINDOW_DAYS):
            raise OperationsError(f"window must be positive and no longer than {MAX_WINDOW_DAYS} days")
        catalog = DatasetCatalog(self.parquet_root)
        path = catalog.path_for(dataset_id)
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise OperationsError("dataset is too large for this node; request a smaller published partition")
        frame = pd.read_parquet(path, columns=["open", "high", "low", "close"])
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise OperationsError("dataset timestamp index is invalid")
        index = _utc_index(frame.index)
        frame.index = index
        return frame.loc[(frame.index >= from_time) & (frame.index <= to_time)]

    def _paper_state(self, *, read_only: bool) -> JsonDict:
        if not self.paper_control_path.exists() and read_only:
            return {"paused": False, "state_present": False, "control_path": self.paper_control_path.name}
        try:
            raw = json.loads(self.paper_control_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"paused": True, "state_present": True, "control_path": self.paper_control_path.name, "healthy": False}
        return {
            "paused": bool(raw.get("paused", False)) if isinstance(raw, dict) else True,
            "state_present": True,
            "control_path": self.paper_control_path.name,
            "healthy": isinstance(raw, dict),
        }

    def _write_paper_pause(self, paused: bool) -> None:
        """Write the exact control_paper.json protocol consumed by btc-qt App."""

        _write_bytes_atomic(
            json.dumps({"paused": paused, "ts": int(datetime.now(timezone.utc).timestamp() * 1_000)}).encode("utf-8"),
            self.paper_control_path,
        )


def build_operations_router(service: OperationsService) -> APIRouter:
    """Create the operations routes; caller supplies the sole ``/api/v3`` prefix."""

    router = APIRouter(tags=["operations"])

    @router.get("/datasets")
    def datasets() -> JsonDict:
        return {"items": service.datasets(), "next_cursor": None}

    @router.get("/sources")
    def sources() -> JsonDict:
        return {"items": service.sources(), "next_cursor": None}

    @router.post("/staging/uploads", status_code=status.HTTP_201_CREATED)
    async def staging_upload(request: Request) -> JsonDict:
        try:
            return await service.stage_upload(request)
        except OperationsError as error:
            raise _api_error(422, "invalid_upload", str(error)) from error

    @router.post("/imports", status_code=status.HTTP_202_ACCEPTED)
    def imports(
        request: ImportRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JsonDict:
        return _submitted_job(response, service.submit_import, request, idempotency_key)

    @router.post("/sync-jobs", status_code=status.HTTP_202_ACCEPTED)
    def sync_jobs(
        request: SyncRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JsonDict:
        return _submitted_job(response, service.submit_sync, request, idempotency_key)

    @router.get("/events/wicks")
    def events_wicks(
        dataset_id: str,
        from_time: Annotated[datetime, Query(alias="from")],
        to_time: Annotated[datetime, Query(alias="to")],
        limit: int = 50,
    ) -> JsonDict:
        try:
            bounded = _limit(limit)
            _require_aware(from_time)
            _require_aware(to_time)
            return {"items": service.events_wicks(dataset_id=dataset_id, from_time=from_time, to_time=to_time, limit=bounded), "next_cursor": None}
        except OperationsError as error:
            raise _api_error(422, "invalid_wick_query", str(error)) from error

    @router.get("/opportunities")
    def opportunities(cursor: str | None = None, limit: int = 50, as_of: datetime | None = None) -> JsonDict:
        try:
            if as_of is not None:
                _require_aware(as_of)
            items, next_cursor, generated_at, generated_at_status = service.opportunities(cursor=cursor, limit=_limit(limit), as_of=as_of)
            return {
                "items": items,
                "next_cursor": next_cursor,
                "generated_at": generated_at,
                "generated_at_status": generated_at_status,
            }
        except OperationsError as error:
            raise _api_error(422, "invalid_opportunity_query", str(error)) from error

    @router.get("/portfolio")
    def portfolio(cursor: str | None = None, limit: int = 50) -> JsonDict:
        try:
            items, next_cursor = service.portfolio(cursor=cursor, limit=_limit(limit))
            return {"items": items, "next_cursor": next_cursor}
        except OperationsError as error:
            raise _api_error(422, "invalid_portfolio_query", str(error)) from error

    @router.get("/runtime")
    def runtime() -> JsonDict:
        return service.runtime()

    @router.post("/runtime/commands")
    def runtime_commands(request: RuntimeCommandRequest) -> JsonDict:
        try:
            return service.runtime_command(request)
        except OperationsError as error:
            raise _api_error(422, "invalid_runtime_command", str(error)) from error

    @router.get("/audit")
    def audit(cursor: str | None = None, limit: int = 50) -> JsonDict:
        try:
            items, next_cursor = service.store.list_audit(cursor=cursor, limit=_limit(limit))
            return {"items": items, "next_cursor": next_cursor}
        except OperationsError as error:
            raise _api_error(422, "invalid_audit_query", str(error)) from error

    return router


def _submitted_job(
    response: Response,
    submit: Callable[[RequestModel, str], tuple[JsonDict, bool]],
    request: RequestModel,
    idempotency_key: str | None,
) -> JsonDict:
    if idempotency_key is None:
        raise _api_error(400, "idempotency_required", "Idempotency-Key is required")
    try:
        job, created = submit(request, idempotency_key)
    except OperationsError as error:
        status_code = 409 if "idempotency key" in str(error) or "queue is full" in str(error) else 422
        code = "idempotency_conflict" if "idempotency key" in str(error) else "capacity_blocked" if "queue is full" in str(error) else "invalid_operation"
        raise _api_error(status_code, code, str(error)) from error
    response.headers["Location"] = f"/api/v3/jobs/{job['job_id']}"
    return {"job": job, "created": created}


def _contained_file(root: Path, object_key: str) -> Path:
    if object_key.startswith(("/", "\\")):
        raise OperationsError("object_key must be a relative staging path")
    candidate = (root / object_key).resolve()
    root_resolved = root.resolve()
    if root_resolved != candidate and root_resolved not in candidate.parents:
        raise OperationsError("object_key escapes the staging root")
    return candidate


def _upload_suffix(content_type: str, declared_format: str | None) -> str:
    if declared_format is not None and declared_format not in {"csv", "parquet"}:
        raise OperationsError("X-Upload-Format must be csv or parquet")
    if declared_format == "csv" or content_type in {"text/csv", "application/csv"}:
        return ".csv"
    if declared_format == "parquet" or content_type in {
        "application/vnd.apache.parquet",
        "application/x-parquet",
        "application/octet-stream",
    }:
        return ".parquet"
    raise OperationsError("Content-Type must identify CSV or Parquet")


def _write_bytes_atomic(content: bytes, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except BaseException:
        if "temporary" in locals():
            temporary.unlink(missing_ok=True)
        raise


def _read_staged_frame(path: Path, format_name: str) -> pd.DataFrame:
    required_columns = {"timestamp", "open", "high", "low", "close", "volume"}
    if format_name == "csv":
        frame = pd.read_csv(
            path,
            nrows=MAX_IMPORT_ROWS + 1,
            usecols=lambda column: column in required_columns,
        )
    elif format_name == "parquet":
        parquet = pq.ParquetFile(path)  # type: ignore[no-untyped-call]
        if parquet.metadata is None or parquet.metadata.num_rows > MAX_IMPORT_ROWS:
            raise OperationsError("import exceeds the 200,000 row limit")
        columns = [column for column in parquet.schema.names if column in required_columns]
        frame = parquet.read(columns=columns).to_pandas()  # type: ignore[no-untyped-call]
    else:
        raise OperationsError("unsupported import format")
    if len(frame) > MAX_IMPORT_ROWS:
        raise OperationsError("import exceeds the one million row limit")
    return frame


def _validated_ohlcv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    candidate = frame.copy()
    if "timestamp" not in candidate.columns:
        raise OperationsError("import requires a timestamp column")
    candidate["timestamp"] = pd.to_datetime(candidate["timestamp"], utc=True, errors="coerce")
    candidate = candidate.set_index("timestamp")
    candidate.index = _utc_index(candidate.index)
    required = ["open", "high", "low", "close", "volume"]
    missing = [name for name in required if name not in candidate.columns]
    if missing:
        raise OperationsError(f"import is missing required columns: {', '.join(missing)}")
    candidate = candidate[required].apply(pd.to_numeric, errors="coerce")
    if candidate.index.hasnans or candidate.isna().any().any():
        raise OperationsError("timestamps and OHLCV values must be present and numeric")
    if len(candidate) == 0:
        raise OperationsError("import contains no rows")
    if len(candidate) > MAX_IMPORT_ROWS:
        raise OperationsError("import exceeds the one million row limit")
    if not candidate.index.is_monotonic_increasing or candidate.index.has_duplicates:
        raise OperationsError("timestamps must be strictly increasing and unique")
    if not bool((candidate["high"] >= candidate[["open", "close", "low"]].max(axis=1)).all()):
        raise OperationsError("high must be at least open, close, and low")
    if not bool((candidate["low"] <= candidate[["open", "close", "high"]].min(axis=1)).all()):
        raise OperationsError("low must be at most open, close, and high")
    if not bool((candidate[["open", "high", "low", "close", "volume"]] >= 0).all().all()):
        raise OperationsError("OHLCV values must be non-negative")
    return candidate.astype("float64")


def _normalized_rows_frame(rows: Sequence[NormalizedRow], *, required_fields: Sequence[str]) -> pd.DataFrame:
    if len(rows) > MAX_IMPORT_ROWS:
        raise OperationsError("provider result exceeds the one million row limit")
    if not rows:
        raise OperationsError("provider returned no records")
    records: list[JsonDict] = []
    for row in rows:
        if row.available_at < row.timestamp:
            raise OperationsError("provider row has available_at before timestamp")
        missing = [field for field in required_fields if field not in row.values]
        if missing:
            raise OperationsError(f"provider row is missing required fields: {', '.join(missing)}")
        records.append(
            {
                "timestamp": row.timestamp,
                "available_at": row.available_at,
                "source": row.source,
                "dataset": row.dataset,
                "symbol": row.symbol,
                **row.values,
            }
        )
    frame = pd.DataFrame(records)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame["available_at"] = pd.to_datetime(frame["available_at"], utc=True, errors="coerce")
    if frame[["timestamp", "available_at"]].isna().any().any():
        raise OperationsError("provider returned invalid timestamps")
    frame = frame.sort_values("timestamp").drop_duplicates(subset="timestamp", keep="last").set_index("timestamp")
    return frame


def _publish_parquet_atomic(frame: pd.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        frame.to_parquet(temporary, compression="zstd")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    digest.update(frame.to_csv(index=True, lineterminator="\n").encode("utf-8"))
    return digest.hexdigest()


def _safe_dataset_key(value: str) -> str:
    normalized = "".join(character if character.isalnum() or character in "_-" else "_" for character in value)
    if not normalized or normalized != value:
        raise OperationsError("dataset identifiers may contain only letters, numbers, underscores, and hyphens")
    return normalized


def _canonical_symbol(value: str) -> str:
    symbol = value.upper()
    if re.fullmatch(r"[A-Z0-9]+(?:[/:_-][A-Z0-9]+)*", symbol) is None:
        raise OperationsError("symbol must be an explicit alphanumeric market identity such as BTC/USDT")
    return symbol


def _symbol_path_key(symbol: str) -> str:
    """Encode canonical market identity injectively; never use it as a path directly."""

    return f"symbol-{symbol.encode('ascii').hex()}"


def _parse_aware_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OperationsError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise OperationsError(f"{name} must include a timezone")
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise OperationsError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _validate_sync_plan(
    plan: Sequence[ProviderPlanItem],
    *,
    source: str,
    symbol: str,
    from_time: datetime,
    to_time: datetime,
) -> None:
    intervals: list[tuple[datetime, datetime]] = []
    for item in plan:
        if item.provider != source or item.symbol != symbol:
            raise OperationsError("provider sync plan identity does not match the request")
        start_utc = _as_utc(item.start)
        end_utc = _as_utc(item.end)
        if end_utc <= start_utc:
            raise OperationsError("provider sync plan contains an invalid time interval")
        intervals.append((start_utc, end_utc))
    covered_until = from_time
    for start, end in sorted(intervals):
        if start > covered_until:
            raise OperationsError("provider sync plan leaves a requested time-range gap")
        if end > covered_until:
            covered_until = end
    if covered_until < to_time:
        raise OperationsError("provider sync plan does not cover the requested time range")


def _coverage_status(frame: pd.DataFrame, *, from_time: datetime, to_time: datetime) -> JsonDict:
    observed_from = _timestamp_string(frame.index[0])
    observed_to = _timestamp_string(frame.index[-1])
    complete = frame.index[0].to_pydatetime() <= from_time and frame.index[-1].to_pydatetime() >= to_time
    return {
        "status": "complete" if complete else "partial",
        "observed_from": observed_from,
        "observed_to": observed_to,
        "resume_required": not complete,
    }


def _limit(value: int) -> int:
    if not 1 <= value <= MAX_LIST_LIMIT:
        raise OperationsError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
    return value


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not cursor.isdigit() or int(cursor) > 1_000_000:
        raise OperationsError("cursor must be a bounded numeric offset")
    return int(cursor)


def _validate_idempotency_key(value: str) -> str:
    key = value.strip()
    if not key or len(key) > _IDEMPOTENCY_MAX_LENGTH:
        raise OperationsError("Idempotency-Key must be a non-empty value up to 256 characters")
    return key


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise OperationsError(f"{key} is required")
    return value


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _json_object(value: str) -> JsonDict:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise OperationsError("stored operation payload is invalid")
    return cast(JsonDict, parsed)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OperationsError("timestamps must include a timezone")


def _timestamp_string(value: object) -> str:
    if isinstance(value, pd.Timestamp):
        return str(value.isoformat())
    if isinstance(value, datetime):
        return value.isoformat()
    raise OperationsError("dataset timestamp is invalid")


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, "request_id": None, "fields": {}})
