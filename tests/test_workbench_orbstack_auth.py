"""Origin auth must also protect direct OrbStack container/documentation ingress."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qt.workbench.api import WorkbenchSettings, create_workbench_app


@pytest.fixture
def protected_client(tmp_path: Path) -> TestClient:
    return TestClient(create_workbench_app(WorkbenchSettings(
        parquet_root=tmp_path / "parquet", state_root=tmp_path / "state",
        origin_client_id="fixture-origin-id", origin_client_secret="fixture-origin-secret",
    )))


@pytest.mark.parametrize("path", ["/api/v3/experiments", "/api/v3/btc-monitor/status", "/docs", "/redoc", "/openapi.json", "/missing-route"])
def test_every_origin_path_rejects_missing_or_forged_headers(protected_client: TestClient, path: str) -> None:
    assert protected_client.get(path).status_code == 401
    assert protected_client.get(path, headers={
        "CF-Access-Client-Id": "browser-spoofed-id", "CF-Access-Client-Secret": "browser-spoofed-secret",
        "X-QT-Access-Subject": "local:unverified",
    }).status_code == 401


def test_valid_origin_headers_need_nonblank_subject(protected_client: TestClient) -> None:
    headers = {"CF-Access-Client-Id": "fixture-origin-id", "CF-Access-Client-Secret": "fixture-origin-secret"}
    assert protected_client.get("/api/v3/experiments", headers=headers).status_code == 401
    assert protected_client.get("/api/v3/experiments", headers=headers | {"X-QT-Access-Subject": "  "}).status_code == 401
    response = protected_client.get("/api/v3/experiments", headers=headers | {"X-QT-Access-Subject": "local:fixture"})
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_existing_loopback_development_without_origin_pair_still_works(tmp_path: Path) -> None:
    client = TestClient(create_workbench_app(WorkbenchSettings(
        parquet_root=tmp_path / "parquet", state_root=tmp_path / "state",
    )))
    assert client.get("/api/v3/experiments").status_code == 200
