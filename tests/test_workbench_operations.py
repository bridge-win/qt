from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import cast

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from qt.legacy.btc_quant_evolution.external_data.schemas import (
    CredentialStatus,
    ExternalDataProvider,
    NormalizedRow,
    ProviderCapability,
    ProviderPlanItem,
    ProviderSyncRequest,
)
from qt.research.repository import ResearchRepository
from qt.research.worker import ResearchWorker
from qt.workbench.operations import (
    OperationsService,
    build_operations_router,
)


class FixtureProvider:
    name = "fixture_public"
    required_credentials: tuple[str, ...] = ()

    def capabilities(self) -> Sequence[ProviderCapability]:
        return (
            ProviderCapability(
                provider=self.name,
                dataset="fear_greed",
                free=True,
                required_credentials=(),
                min_timeframe="1d",
                required_fields=("fear_greed_value",),
            ),
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        return [
            ProviderPlanItem(
                provider=self.name,
                dataset="fear_greed",
                symbol=request.symbol,
                start=request.start_as_datetime(),
                end=request.end_as_datetime(),
                params={"fixture": "yes"},
            )
        ]

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        return [{"value": 23.0}]

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        return [
            NormalizedRow(
                timestamp=item.start,
                available_at=item.start + timedelta(days=1),
                source=self.name,
                dataset=item.dataset,
                symbol=item.symbol,
                values={"fear_greed_value": 23.0},
            )
        ]


def _client(
    tmp_path: Path,
    *,
    queue_limit: int = 20,
    providers: Sequence[ExternalDataProvider] | None = None,
) -> tuple[TestClient, OperationsService]:
    state_root = tmp_path / "state"
    service = OperationsService(
        research_repository=ResearchRepository(state_root / "research.sqlite3", queue_limit=queue_limit),
        state_root=state_root,
        parquet_root=tmp_path / "parquet",
        providers=providers or (FixtureProvider(),),
    )
    app = FastAPI()
    app.include_router(build_operations_router(service), prefix="/api/v3")
    return TestClient(app), service


def _run_shared_job(service: OperationsService, job_id: str) -> dict[str, object]:
    worker = ResearchWorker(
        service.research_repository,
        worker_id="fixture-worker",
        executor=service.execute,
    )
    assert worker.run_once() is True
    return service.research_repository.get_job(job_id)


def test_import_rejects_traversal_and_enforces_real_idempotency(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    rejected = client.post(
        "/api/v3/imports",
        headers={"Idempotency-Key": "traversal"},
        json={"source": "staging", "dataset_id": "safe", "format": "csv", "object_key": "../secret.csv"},
    )
    assert rejected.status_code == 422
    too_large = client.post(
        "/api/v3/staging/uploads",
        headers={"content-type": "text/csv"},
        content=b"x" * (1024 * 1024 + 1),
    )
    assert too_large.status_code == 422

    upload = client.post(
        "/api/v3/staging/uploads",
        headers={"content-type": "text/csv"},
        content="timestamp,open,high,low,close,volume\n2024-01-01T00:00:00Z,10,12,9,11,2\n",
    )
    assert upload.status_code == 201
    assert upload.json()["object_key"].startswith("uploads/")
    payload = {"source": "staging", "dataset_id": "safe", "format": "csv", "object_key": upload.json()["object_key"]}
    first = client.post("/api/v3/imports", headers={"Idempotency-Key": "same"}, json=payload)
    second = client.post("/api/v3/imports", headers={"Idempotency-Key": "same"}, json=payload)
    changed = client.post(
        "/api/v3/imports",
        headers={"Idempotency-Key": "same"},
        json={**payload, "dataset_id": "different"},
    )
    assert first.status_code == 202
    assert second.json()["job"]["job_id"] == first.json()["job"]["job_id"]
    assert second.json()["created"] is False
    assert changed.status_code == 409


def test_staging_upload_rejects_chunked_oversize_and_returns_opaque_key(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    def oversized_stream() -> object:
        yield b"timestamp,open,high,low,close,volume\n"
        yield b"x" * (1024 * 1024)

    rejected = client.post(
        "/api/v3/staging/uploads",
        headers={"content-type": "text/csv"},
        content=oversized_stream(),
    )
    accepted = client.post(
        "/api/v3/staging/uploads",
        headers={"content-type": "text/csv"},
        content="timestamp,open,high,low,close,volume\n2024-01-01T00:00:00Z,10,12,9,11,2\n",
    )
    assert rejected.status_code == 422
    assert accepted.status_code == 201
    object_key = accepted.json()["object_key"]
    assert object_key.startswith("uploads/")
    assert (tmp_path / "state" / "staging" / object_key).is_file()


def test_staging_upload_stops_at_limit_without_consuming_later_chunks(tmp_path: Path) -> None:
    _, service = _client(tmp_path)
    consumed: list[int] = []

    class StreamingRequest:
        def __init__(self) -> None:
            self.headers = {"content-type": "text/csv"}

        async def stream(self) -> object:
            for chunk in (b"a" * 524_288, b"b" * 524_288, b"c", b"never-consumed"):
                consumed.append(len(chunk))
                yield chunk

    with pytest.raises(Exception, match="edge-safe limit"):
        asyncio.run(service.stage_upload(cast(Request, StreamingRequest())))
    assert consumed == [524_288, 524_288, 1]


def test_queue_capacity_and_import_worker_publish(tmp_path: Path) -> None:
    client, service = _client(tmp_path, queue_limit=1)
    upload = client.post(
        "/api/v3/staging/uploads",
        headers={"content-type": "text/csv"},
        content="timestamp,open,high,low,close,volume\n2024-01-01T00:00:00Z,10,12,9,11,2\n",
    )
    payload = {"source": "staging", "dataset_id": "safe", "format": "csv", "object_key": upload.json()["object_key"]}
    queued = client.post("/api/v3/imports", headers={"Idempotency-Key": "one"}, json=payload)
    full = client.post("/api/v3/imports", headers={"Idempotency-Key": "two"}, json=payload)
    assert queued.status_code == 202
    assert full.status_code == 409

    result = _run_shared_job(service, str(queued.json()["job"]["job_id"]))
    assert result["status"] == "complete"
    assert result["result"] == {
        "dataset_id": "safe",
        "path": "ohlcv/safe.parquet",
        "rows": 1,
        "fingerprint": result["result"]["fingerprint"],
        "quality": {"accepted": True, "required_columns": ["open", "high", "low", "close", "volume"]},
    }
    assert (tmp_path / "parquet" / "ohlcv" / "safe.parquet").exists()


def test_sync_worker_uses_provider_normalization_and_publishes(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    response = client.post(
        "/api/v3/sync-jobs",
        headers={"Idempotency-Key": "sync-public"},
        json={
            "source": "fixture_public",
            "dataset_id": "fear_greed",
            "symbol": "BTC",
            "timeframe": "1d",
            "from": "2024-01-01T00:00:00Z",
            "to": "2024-01-03T00:00:00Z",
        },
    )
    assert response.status_code == 202
    completed = _run_shared_job(service, str(response.json()["job"]["job_id"]))
    assert completed["status"] == "complete"
    path = tmp_path / "parquet" / "fear_greed" / "fixture_public_symbol-425443_1d.parquet"
    frame = pd.read_parquet(path)
    assert frame.iloc[0]["available_at"] > frame.index[0]
    assert frame.iloc[0]["fear_greed_value"] == 23.0


def test_sync_executes_all_bounded_plan_items_and_encodes_market_symbol(tmp_path: Path) -> None:
    class MultiPlanProvider(FixtureProvider):
        def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
            start = request.start_as_datetime()
            middle = start + timedelta(days=1)
            return [
                ProviderPlanItem(self.name, "fear_greed", request.symbol, start, middle, {"page": "1"}),
                ProviderPlanItem(self.name, "fear_greed", request.symbol, middle, request.end_as_datetime(), {"page": "2"}),
            ]

    client, service = _client(tmp_path, providers=(MultiPlanProvider(),))
    response = client.post(
        "/api/v3/sync-jobs",
        headers={"Idempotency-Key": "sync-multi"},
        json={
            "source": "fixture_public",
            "dataset_id": "fear_greed",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "from": "2024-01-01T00:00:00Z",
            "to": "2024-01-03T00:00:00Z",
        },
    )
    assert response.status_code == 202
    completed = _run_shared_job(service, str(response.json()["job"]["job_id"]))
    assert completed["status"] == "complete"
    quality = completed["result"]["quality"]
    assert quality["plan_items_executed"] == 2
    assert (tmp_path / "parquet" / "fear_greed" / "fixture_public_symbol-4254432f55534454_1h.parquet").exists()


def test_opportunities_and_portfolio_read_persisted_state(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    runtime = tmp_path / "state" / "runtime"
    (runtime / "intel").mkdir(parents=True)
    (runtime / "intel" / "opportunities.json").write_text(
        json.dumps({"generated_at": "2024-01-01T00:00:00+00:00", "count": 1, "opportunities": [{"kind": "funding", "why": "funding dislocation"}]})
    )
    ledger = runtime / "portfolios" / "wick"
    ledger.mkdir(parents=True)
    (ledger / "state.json").write_text(json.dumps({"cash": 1000.0, "positions": {"BTC/USDT": 0.01}}))

    opportunities = client.get("/api/v3/opportunities?limit=1")
    portfolio = client.get("/api/v3/portfolio?limit=1")
    assert opportunities.status_code == 200
    assert opportunities.json()["items"][0]["explanation"] == "funding dislocation"
    assert portfolio.status_code == 200
    assert portfolio.json()["items"][0]["positions"] == {"BTC/USDT": 0.01}


def test_opportunities_compare_aware_times_and_report_invalid_persisted_time(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    path = tmp_path / "state" / "runtime" / "intel"
    path.mkdir(parents=True)
    target = path / "opportunities.json"
    target.write_text(
        json.dumps({"generated_at": "2024-01-01T01:00:00+02:00", "opportunities": [{"why": "valid"}]}),
    )
    response = client.get("/api/v3/opportunities?as_of=2024-01-01T00:00:00Z")
    assert response.status_code == 200
    assert response.json()["items"]
    assert response.json()["generated_at_status"] == "valid"
    target.write_text(json.dumps({"generated_at": "not-a-time", "opportunities": [{"why": "unknown"}]}))
    invalid = client.get("/api/v3/opportunities?as_of=2024-01-01T00:00:00Z")
    assert invalid.json()["items"] == []
    assert invalid.json()["generated_at_status"] == "invalid"


def test_runtime_rejects_live_or_unknown_controls_and_audits_paper_pause(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    forbidden = client.post("/api/v3/runtime/commands", json={"command": "live.enable"})
    unknown = client.post("/api/v3/runtime/commands", json={"command": "paper.flatten"})
    paused = client.post("/api/v3/runtime/commands", json={"command": "paper.pause", "reason": "operator check"})
    audit = client.get("/api/v3/audit?limit=1")
    assert forbidden.status_code == 422
    assert unknown.status_code == 422
    assert paused.status_code == 200
    assert paused.json()["runtime"]["live_enabled"] is False
    assert paused.json()["runtime"]["paper"]["paused"] is True
    assert json.loads((tmp_path / "state" / "control_paper.json").read_text())["paused"] is True
    assert audit.json()["items"][0]["command"] == "paper.pause"


def test_wick_request_rejects_unbounded_or_oversized_windows(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    missing_range = client.get("/api/v3/events/wicks?dataset_id=okx-btcusdt-1h")
    oversized = client.get(
        "/api/v3/events/wicks?dataset_id=okx-btcusdt-1h&from=2023-01-01T00:00:00Z&to=2025-01-02T00:00:00Z&limit=201"
    )
    assert missing_range.status_code == 422
    assert oversized.status_code == 422
