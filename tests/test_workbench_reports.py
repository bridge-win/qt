from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

import pytest
from fastapi.testclient import TestClient

from qt.workbench.api import WorkbenchSettings, create_workbench_app
from qt.workbench.reports import R2ReportPublisher, R2ReportSettings, ReportPublicationError


class _ObjectStore:
    def __init__(self) -> None:
        self.uploads: list[tuple[bytes, str, str, dict[str, object]]] = []

    def upload_fileobj(
        self, fileobj: BinaryIO, bucket: str, key: str, extra_args: Mapping[str, object]
    ) -> None:
        self.uploads.append((fileobj.read(), bucket, key, dict(extra_args)))


def _native_result(path: Path) -> dict[str, object]:
    encoded = path.read_bytes()
    return {
        "run_id": "run-1",
        "artifacts": [
            {
                "name": "equity.json",
                "path": str(path),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "size_bytes": len(encoded),
                "media_type": "application/json",
                "rows": 3,
            }
        ],
    }


def _first_artifact(payload: Mapping[str, object]) -> dict[str, object]:
    artifacts = payload.get("artifacts")
    assert isinstance(artifacts, list) and artifacts
    artifact = artifacts[0]
    assert isinstance(artifact, dict)
    return artifact


def test_report_publisher_marks_only_uploaded_artifacts_as_downloadable(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact = artifact_root / "run-1" / "equity.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    local = R2ReportPublisher(None, artifact_root=artifact_root).publish_result(_native_result(artifact))
    local_record = _first_artifact(local)
    assert local_record["publication_status"] == "r2_not_configured"
    assert local_record["r2_key"] is None
    assert "path" not in local_record
    assert "_attested_path" not in local_record

    store = _ObjectStore()
    publisher = R2ReportPublisher(
        R2ReportSettings("https://account.r2.cloudflarestorage.com", "reports", "id", "secret"),
        artifact_root=artifact_root,
        client_factory=lambda _settings: store,
    )
    published = publisher.publish_result(_native_result(artifact))
    assert store.uploads[0][1:3] == ("reports", "reports/run-1/equity.json")
    assert store.uploads[0][0] == b"{}"
    assert _first_artifact(published)["publication_status"] == "published"


@pytest.mark.parametrize("kind", ["outside", "hash_mismatch", "size_mismatch"])
def test_report_publisher_rejects_unattested_artifact_descriptors(tmp_path: Path, kind: str) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact = artifact_root / "run-1" / "equity.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    result = _native_result(artifact)
    record = _first_artifact(result)
    if kind == "outside":
        secret = tmp_path / "secret.env"
        secret.write_text("token=do-not-upload", encoding="utf-8")
        record["path"] = str(secret)
        record["sha256"] = hashlib.sha256(secret.read_bytes()).hexdigest()
        record["size_bytes"] = secret.stat().st_size
    elif kind == "hash_mismatch":
        record["sha256"] = "0" * 64
    else:
        record["size_bytes"] = 999

    store = _ObjectStore()
    publisher = R2ReportPublisher(
        R2ReportSettings("https://account.r2.cloudflarestorage.com", "reports", "id", "secret"),
        artifact_root=artifact_root,
        client_factory=lambda _settings: store,
    )
    with pytest.raises(ReportPublicationError):
        publisher.publish_result(result)
    assert store.uploads == []


def test_report_publisher_rejects_symlinked_artifact(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact = artifact_root / "run-1" / "equity.json"
    secret = tmp_path / "secret.env"
    artifact.parent.mkdir(parents=True)
    secret.write_text("token=do-not-upload", encoding="utf-8")
    artifact.symlink_to(secret)
    result = _native_result(artifact)

    with pytest.raises(ReportPublicationError, match="symlink"):
        R2ReportPublisher(None, artifact_root=artifact_root).publish_result(result)


def test_report_publisher_rejects_special_file(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    special = artifact_root / "run-1" / "stream"
    special.parent.mkdir(parents=True)
    os.mkfifo(special)
    result = {
        "run_id": "run-1",
        "artifacts": [
            {
                "name": "stream",
                "path": str(special),
                "sha256": "0" * 64,
                "size_bytes": 0,
            }
        ],
    }

    with pytest.raises(ReportPublicationError, match="regular file"):
        R2ReportPublisher(None, artifact_root=artifact_root).publish_result(result)


def test_report_publisher_accepts_only_server_rebased_plugin_artifacts(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact = artifact_root / "plugins" / "run-1" / "artifacts" / "run-1" / "equity.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    result = _native_result(artifact)
    descriptor = _first_artifact(result)
    descriptor["path"] = "artifacts/run-1/equity.json"
    result["plugin_runtime"] = {"temporal_integrity": "unverified"}

    published = R2ReportPublisher(None, artifact_root=artifact_root).publish_result(result)
    record = _first_artifact(published)
    assert record["publication_status"] == "r2_not_configured"
    assert record["media_type"] == "application/json"


def test_report_publisher_rejects_plugin_descriptor_traversal(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact = artifact_root / "plugins" / "run-1" / "artifacts" / "run-1" / "equity.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    result = _native_result(artifact)
    descriptor = _first_artifact(result)
    descriptor["path"] = "../../../../secret.env"
    result["plugin_runtime"] = {"temporal_integrity": "unverified"}

    with pytest.raises(ReportPublicationError):
        R2ReportPublisher(None, artifact_root=artifact_root).publish_result(result)


def test_report_publisher_sanitizes_untrusted_media_type_and_duplicate_names(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact = artifact_root / "run-1" / "equity.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    result = _native_result(artifact)
    descriptor = _first_artifact(result)
    descriptor["media_type"] = {"untrusted": "object"}
    local = R2ReportPublisher(None, artifact_root=artifact_root).publish_result(result)
    assert _first_artifact(local)["media_type"] == "application/octet-stream"

    result["artifacts"] = [descriptor, dict(descriptor)]
    with pytest.raises(ReportPublicationError, match="duplicate artifact name"):
        R2ReportPublisher(None, artifact_root=artifact_root).publish_result(result)


def test_artifact_api_reads_lab_result_envelope_and_emits_only_published_url(tmp_path: Path) -> None:
    app = create_workbench_app(
        WorkbenchSettings(parquet_root=tmp_path / "parquet", state_root=tmp_path / "state")
    )
    app.state.lab_service.import_native_result(
        {
            "run_id": "run-2",
            "artifacts": [
                {
                    "name": "equity.json",
                    "path": "/private/local/equity.json",
                    "publication_status": "published",
                    "r2_key": "reports/run-2/equity.json",
                },
                {
                    "name": "orders.csv",
                    "path": "/private/local/orders.csv",
                    "publication_status": "r2_not_configured",
                    "r2_key": None,
                },
                {
                    "name": "untrusted.json",
                    "publication_status": "published",
                    "r2_key": "reports/other-run/untrusted.json",
                },
            ],
        }
    )
    client = TestClient(app)
    listed = client.get("/api/v3/artifacts")
    assert listed.status_code == 200
    records = {item["name"]: item for item in listed.json()["items"]}
    assert records["equity.json"]["download_url"] == "/artifacts/run-2/equity.json"
    assert records["orders.csv"]["download_url"] is None
    assert records["untrusted.json"]["download_url"] is None
    detail = client.get("/api/v3/artifacts/run-2:equity.json")
    assert detail.status_code == 200
    assert detail.json()["published"] is True
    assert "/private/local" not in detail.text
