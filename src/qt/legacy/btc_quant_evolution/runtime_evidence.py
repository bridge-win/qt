# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.strategy_provenance import SOTA_RUNTIME_SOURCE_FILES

DRY_RUN_RECEIPT_FILENAME = "dry_run_launch_receipt.json"
DRY_RUN_RECEIPT_SCHEMA_VERSION = 1


def load_sota_runtime_binding(
    *,
    root_dir: Path,
    candidate_package_path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    root_dir = root_dir.resolve()
    package_path = _require_file_within_root(root_dir, candidate_package_path)
    package = json.loads(package_path.read_text())
    if not isinstance(package, dict) or package.get("schema_version") != 3:
        raise ValueError("candidate package is not schema-3")
    if package.get("strategy") != "Sota":
        raise ValueError("candidate package strategy is not Sota")
    candidate_id = package.get("candidate_id")
    source_artifacts = package.get("source_artifacts")
    runtime_sources = package.get("runtime_strategy_sources")
    if (
        not isinstance(candidate_id, str)
        or not isinstance(source_artifacts, dict)
        or not isinstance(runtime_sources, dict)
        or set(runtime_sources) != set(SOTA_RUNTIME_SOURCE_FILES)
    ):
        raise ValueError("candidate package runtime selection is malformed")
    optimized_params = _artifact(source_artifacts, "optimized_params")
    feature_matrix = _artifact(source_artifacts, "runtime_feature_matrix")
    typed_sources = {
        name: _artifact(runtime_sources, name)
        for name in SOTA_RUNTIME_SOURCE_FILES
    }
    for artifact in (optimized_params, feature_matrix, *typed_sources.values()):
        _validate_recorded_artifact(root_dir, artifact)
    selection = {
        "candidate_id": candidate_id,
        "candidate_package_artifact": file_artifact(root_dir=root_dir, path=package_path),
        "feature_matrix_artifact": feature_matrix,
        "optimized_params_artifact": optimized_params,
        "strategy": "Sota",
    }
    return selection, typed_sources


def file_artifact(*, root_dir: Path, path: Path) -> dict[str, Any]:
    root_dir = root_dir.resolve()
    resolved = path.resolve() if path.is_absolute() else (root_dir / path).resolve()
    try:
        relative = resolved.relative_to(root_dir)
    except ValueError as exc:
        raise ValueError("runtime evidence artifact is outside root directory") from exc
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise ValueError("runtime evidence artifact is missing or empty")
    return {
        "exists": True,
        "path": relative.as_posix(),
        "sha256": _sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def prepare_dry_run_launch_receipt(
    *,
    root_dir: Path,
    candidate_package_path: Path,
    config_path: Path,
    log_path: Path,
) -> tuple[Path, dict[str, Any]]:
    root_dir = root_dir.resolve()
    package_path = _require_file_within_root(root_dir, candidate_package_path)
    receipt_path = _dry_run_receipt_path(root_dir, package_path)
    runtime_selection, runtime_sources = load_sota_runtime_binding(
        root_dir=root_dir,
        candidate_package_path=package_path,
    )
    resolved_log = _resolve_within_root(root_dir, log_path, "dry-run log")
    binding = {
        "config_artifact": file_artifact(root_dir=root_dir, path=config_path),
        "dry_run": True,
        "log_path": resolved_log.relative_to(root_dir).as_posix(),
        "runtime_selection": runtime_selection,
        "runtime_strategy_sources": runtime_sources,
        "schema_version": DRY_RUN_RECEIPT_SCHEMA_VERSION,
        "strategy": "Sota",
    }
    return receipt_path, binding


def dry_run_receipt_path(*, root_dir: Path, candidate_package_path: Path) -> Path:
    root_dir = root_dir.resolve()
    package_path = _require_file_within_root(root_dir, candidate_package_path)
    return _dry_run_receipt_path(root_dir, package_path)


def validate_dry_run_launch_receipt(
    *,
    root_dir: Path,
    receipt_path: Path,
    candidate_package_path: Path,
    config_path: Path,
    log_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root_dir = root_dir.resolve()
    expected_path, expected_binding = prepare_dry_run_launch_receipt(
        root_dir=root_dir,
        candidate_package_path=candidate_package_path,
        config_path=config_path,
        log_path=log_path,
    )
    resolved_receipt = _resolve_within_root(root_dir, receipt_path, "dry-run launch receipt")
    if resolved_receipt != expected_path or not resolved_receipt.is_file():
        raise ValueError("schema-3 evidence does not match dry-run launch receipt")
    try:
        payload = json.loads(resolved_receipt.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("schema-3 evidence does not match dry-run launch receipt") from exc
    if not isinstance(payload, dict):
        raise ValueError("schema-3 evidence does not match dry-run launch receipt")
    recorded_binding = {
        key: payload.get(key)
        for key in expected_binding
    }
    if recorded_binding != expected_binding:
        raise ValueError("schema-3 evidence does not match dry-run launch receipt")
    started_at = _parse_timestamp(payload.get("launch_started_at"))
    completed_at = _parse_timestamp(payload.get("launch_completed_at"))
    if started_at is None or completed_at is None or completed_at < started_at:
        raise ValueError("schema-3 evidence does not match dry-run launch receipt")
    if resolved_receipt.stat().st_mode & 0o222:
        raise ValueError("schema-3 evidence does not match dry-run launch receipt")
    return payload, file_artifact(root_dir=root_dir, path=resolved_receipt)


def load_dry_run_manifest_anchor(
    *,
    root_dir: Path,
    candidate_package_path: Path,
    receipt_path: Path,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    root_dir = root_dir.resolve()
    package_path = _require_file_within_root(root_dir, candidate_package_path)
    expected_receipt = _dry_run_receipt_path(root_dir, package_path)
    resolved_receipt = _resolve_within_root(root_dir, receipt_path, "dry-run launch receipt")
    if resolved_receipt != expected_receipt:
        raise ValueError("production manifest dry-run receipt path does not match candidate package")
    run_dir = package_path.parent.parent
    manifest_path = run_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("schema-3 dry-run production manifest is missing or malformed") from exc
    phases = manifest.get("phases") if isinstance(manifest, dict) else None
    dry_run = phases.get("dry-run") if isinstance(phases, dict) else None
    outputs = dry_run.get("outputs") if isinstance(dry_run, dict) else None
    current_artifact = file_artifact(root_dir=root_dir, path=resolved_receipt)
    matching_outputs = [
        output
        for output in outputs or []
        if isinstance(output, dict) and output.get("path") == current_artifact["path"]
    ]
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 2
        or manifest.get("run_id") != run_dir.name
        or not isinstance(dry_run, dict)
        or dry_run.get("status") != "passed"
        or len(matching_outputs) != 1
        or matching_outputs[0] != current_artifact
    ):
        raise ValueError("production manifest dry-run receipt artifact does not match current file")
    phase_started = _parse_timestamp(dry_run.get("started_at"))
    phase_completed = _parse_timestamp(dry_run.get("completed_at"))
    launch_started = _parse_timestamp(receipt.get("launch_started_at"))
    launch_completed = _parse_timestamp(receipt.get("launch_completed_at"))
    if (
        phase_started is None
        or phase_completed is None
        or launch_started is None
        or launch_completed is None
        or not phase_started <= launch_started <= launch_completed <= phase_completed
    ):
        raise ValueError("dry-run launch interval does not match production manifest")
    return {
        "dry_run_phase_completed_at": dry_run["completed_at"],
        "dry_run_phase_started_at": dry_run["started_at"],
        "launch_completed_at": receipt["launch_completed_at"],
        "launch_started_at": receipt["launch_started_at"],
        "manifest_path": manifest_path.relative_to(root_dir).as_posix(),
        "receipt_artifact": current_artifact,
        "run_id": run_dir.name,
    }


def write_dry_run_launch_receipt(
    *,
    root_dir: Path,
    receipt_path: Path,
    binding: dict[str, Any],
    launch_started_at: datetime,
    launch_completed_at: datetime,
) -> Path:
    if launch_started_at.tzinfo is None or launch_completed_at.tzinfo is None:
        raise ValueError("dry-run launch timestamps must be timezone-aware")
    if launch_completed_at < launch_started_at:
        raise ValueError("dry-run launch completion precedes start")
    root_dir = root_dir.resolve()
    config_artifact = binding.get("config_artifact")
    if not isinstance(config_artifact, dict) or not isinstance(config_artifact.get("path"), str):
        raise ValueError("dry-run receipt config artifact is malformed")
    if file_artifact(root_dir=root_dir, path=Path(config_artifact["path"])) != config_artifact:
        raise ValueError("dry-run config changed during launch")
    _validate_runtime_binding_artifacts(root_dir, binding)
    payload = {
        **binding,
        "launch_completed_at": _format_timestamp(launch_completed_at),
        "launch_started_at": _format_timestamp(launch_started_at),
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{receipt_path.name}.",
        dir=receipt_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, receipt_path)
        receipt_path.chmod(0o444)
    finally:
        temporary_path.unlink(missing_ok=True)
    return receipt_path


def _dry_run_receipt_path(root_dir: Path, package_path: Path) -> Path:
    relative_parent = package_path.parent.relative_to(root_dir)
    parts = relative_parent.parts
    if (
        len(parts) != 4
        or parts[0] != "user_data"
        or parts[1] != "production_runs"
        or not parts[2]
        or parts[3] != "artifacts"
    ):
        raise ValueError("schema-3 candidate package is outside production run artifacts")
    return package_path.with_name(DRY_RUN_RECEIPT_FILENAME)


def _resolve_within_root(root_dir: Path, path: Path, label: str) -> Path:
    resolved = path.resolve() if path.is_absolute() else (root_dir / path).resolve()
    try:
        resolved.relative_to(root_dir)
    except ValueError as exc:
        raise ValueError(f"{label} is outside root directory") from exc
    return resolved


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validate_runtime_binding_artifacts(root_dir: Path, binding: dict[str, Any]) -> None:
    selection = binding.get("runtime_selection")
    runtime_sources = binding.get("runtime_strategy_sources")
    if not isinstance(selection, dict) or not isinstance(runtime_sources, dict):
        raise ValueError("dry-run runtime selection changed during launch")
    artifacts = [
        selection.get("candidate_package_artifact"),
        selection.get("feature_matrix_artifact"),
        selection.get("optimized_params_artifact"),
        *runtime_sources.values(),
    ]
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("dry-run runtime selection changed during launch")
        path_text = artifact.get("path")
        if not isinstance(path_text, str):
            raise ValueError("dry-run runtime selection changed during launch")
        if file_artifact(root_dir=root_dir, path=Path(path_text)) != artifact:
            raise ValueError("dry-run runtime selection changed during launch")


def _artifact(container: dict[str, Any], name: str) -> dict[str, Any]:
    value = container.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"candidate package {name} artifact is malformed")
    return value


def _validate_recorded_artifact(root_dir: Path, artifact: dict[str, Any]) -> None:
    path_text = artifact.get("path")
    if not isinstance(path_text, str):
        raise ValueError("candidate package runtime artifact is malformed")
    current = file_artifact(root_dir=root_dir, path=Path(path_text))
    if current != artifact:
        raise ValueError("candidate package runtime artifact does not match current file")


def _require_file_within_root(root_dir: Path, path: Path) -> Path:
    resolved = path.resolve() if path.is_absolute() else (root_dir / path).resolve()
    try:
        resolved.relative_to(root_dir)
    except ValueError as exc:
        raise ValueError("candidate package is outside root directory") from exc
    if not resolved.is_file():
        raise ValueError("candidate package is missing")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

