# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import pandas as pd

from qt.legacy.btc_quant_evolution.external_data.schemas import CredentialStatus, NormalizedRow

_REQUIRED_COLUMNS = ("timestamp", "available_at", "source", "dataset", "symbol")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?P<label>\b(?:[A-Za-z_][A-Za-z0-9_]*(?:API[_-]?KEY|[_-]KEY|TOKEN|SECRET|PASSWORD)|"
    r"token|secret|password|api[_-]?key)\b|\bauthorization\b)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_SENSITIVE_BEARER = re.compile(r"(?P<label>\bbearer\b)(?P<separator>\s+)(?P<value>[^\s,;]+)", re.IGNORECASE)
_SENSITIVE_TOKEN = re.compile(
    r"(?P<label>\b(?:token|secret|password|api[_-]?key)\b)(?P<separator>\s+)"
    r"(?P<value>(?!(?:is|missing|unset|provided|required|available)\b)[^\s,;]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExternalDataArtifact:
    path: Path
    sha256: str
    row_count: int
    provider: str
    dataset: str
    symbol: str
    size_bytes: int


def write_jsonl_rows(
    root_dir: Path,
    rows: Sequence[NormalizedRow],
    *,
    provider: str,
    dataset: str,
    symbol: str,
    failure_run_id: str | None = None,
    run_id: str | None = None,
) -> ExternalDataArtifact:
    serialized = "".join(
        json.dumps(
            _row_payload(row),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in sorted(rows, key=_row_sort_key)
    )
    content = serialized.encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    path = (
        _artifact_root(root_dir, run_id=run_id, failure_run_id=failure_run_id)
        / "raw"
        / provider
        / dataset
        / symbol
        / f"{digest}.jsonl"
    )
    _write_bytes_atomic(path, content)
    return ExternalDataArtifact(
        path=path,
        sha256=digest,
        row_count=len(rows),
        provider=provider,
        dataset=dataset,
        symbol=symbol,
        size_bytes=path.stat().st_size,
    )


def write_parquet_rows(
    root_dir: Path,
    rows: Sequence[NormalizedRow],
    *,
    provider: str,
    dataset: str,
    symbol: str,
    timeframe: str,
    failure_run_id: str | None = None,
    run_id: str | None = None,
) -> ExternalDataArtifact:
    directory = (
        _artifact_root(root_dir, run_id=run_id, failure_run_id=failure_run_id)
        / "parquet"
        / provider
        / dataset
        / symbol
    )
    directory.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([_row_payload(row) for row in sorted(rows, key=_row_sort_key)])
    if not rows:
        frame = pd.DataFrame(columns=_REQUIRED_COLUMNS)
    else:
        value_columns = sorted(column for column in frame.columns if column not in _REQUIRED_COLUMNS)
        frame = frame.reindex(columns=[*_REQUIRED_COLUMNS, *value_columns])
    for column in ("timestamp", "available_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True, format="mixed")
    temporary_path = _temporary_path(directory, f"{timeframe}.parquet")
    try:
        frame.to_parquet(temporary_path, index=False)
        _fsync_file(temporary_path)
        digest = _sha256(temporary_path)
        path = directory / f"{timeframe}-{digest}.parquet"
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return ExternalDataArtifact(
        path=path,
        sha256=digest,
        row_count=len(rows),
        provider=provider,
        dataset=dataset,
        symbol=symbol,
        size_bytes=path.stat().st_size,
    )


def write_provider_manifest(
    root_dir: Path,
    *,
    run_id: str,
    artifact: ExternalDataArtifact,
    status: CredentialStatus,
    message: str | None,
    coverage: dict[str, object] | None = None,
    normalized_artifact: ExternalDataArtifact | None = None,
) -> Path:
    path = root_dir / "user_data" / "external_data" / "manifests" / run_id / f"{artifact.provider}-{artifact.dataset}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact": {
            **asdict(artifact),
            "path": artifact.path.relative_to(root_dir).as_posix(),
        },
        "artifacts": {
            "raw": _artifact_payload(root_dir, artifact),
            **(
                {"parquet": _artifact_payload(root_dir, normalized_artifact)}
                if normalized_artifact is not None
                else {}
            ),
        },
        "status": status,
        "message": _redact_message(message),
    }
    if coverage is not None:
        payload["coverage"] = coverage
    _write_bytes_atomic(
        path,
        (
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
                default=str,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8"),
    )
    return path


def _artifact_payload(root_dir: Path, artifact: ExternalDataArtifact) -> dict[str, object]:
    return {
        "path": artifact.path.relative_to(root_dir).as_posix(),
        "row_count": artifact.row_count,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
    }


def _row_payload(row: NormalizedRow) -> dict[str, object]:
    payload = dict(row.values)
    payload.update(
        {
            "timestamp": row.timestamp.isoformat(),
            "available_at": row.available_at.isoformat(),
            "source": row.source,
            "dataset": row.dataset,
            "symbol": row.symbol,
        }
    )
    return payload


def _row_sort_key(row: NormalizedRow) -> tuple[str, str, str, str, str, str]:
    return (
        row.timestamp.isoformat(),
        row.available_at.isoformat(),
        row.source,
        row.dataset,
        row.symbol,
        json.dumps(row.values, sort_keys=True, separators=(",", ":"), allow_nan=False),
    )


def _redact_message(message: str | None) -> str | None:
    if message is None:
        return None

    redacted = _SENSITIVE_ASSIGNMENT.sub(_redact_match, message)
    redacted = _SENSITIVE_BEARER.sub(_redact_match, redacted)
    return _SENSITIVE_TOKEN.sub(_redact_match, redacted)


def _redact_match(match: re.Match[str]) -> str:
    return f"{match.group('label')}{match.group('separator')}<redacted>"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_root(
    root_dir: Path,
    *,
    run_id: str | None,
    failure_run_id: str | None,
) -> Path:
    if run_id is not None and failure_run_id is not None:
        raise ValueError("run_id and failure_run_id are mutually exclusive")
    base = root_dir / "user_data" / "external_data"
    scope = run_id or failure_run_id
    return base / "artifacts" / scope if scope is not None else base / "content"


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_path(path.parent, path.name)
    try:
        with temporary_path.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _temporary_path(directory: Path, name: str) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=f"{name}.tmp-",
    )
    os.close(descriptor)
    return Path(temporary_name)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

