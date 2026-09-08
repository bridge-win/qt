# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from qt.legacy.btc_quant_evolution.candidate_package import validate_sota_rolling_sources
from qt.legacy.btc_quant_evolution.dry_run_evidence import validate_schema_three_dry_run_evidence
from qt.legacy.btc_quant_evolution.freqtrade_strategies.sota_params import (
    validate_sota_candidate_parameters,
)
from qt.legacy.btc_quant_evolution.live_readiness import (
    DEFAULT_MIN_DRY_RUN_DAYS,
    REQUIRED_EVIDENCE,
    validate_readiness_source_payload,
)
from qt.legacy.btc_quant_evolution.runtime_evidence import (
    dry_run_receipt_path,
    load_sota_runtime_binding,
    prepare_dry_run_launch_receipt,
    write_dry_run_launch_receipt,
)
from qt.legacy.btc_quant_evolution.strategy_provenance import SOTA_RUNTIME_SOURCE_FILES

_LEGACY_STRATEGY = "BtcMultiSourceRegimeStrategy"
_SOTA_STRATEGY = "Sota"
_EXCHANGE_KEY = "FREQTRADE__EXCHANGE__KEY"
_EXCHANGE_SECRET = "FREQTRADE__EXCHANGE__SECRET"
_NON_PERSISTENT_RUNTIME = "BTC_QUANT_NON_PERSISTENT_RUNTIME"
_LIVE_READINESS_CHECKS = ("candidate_package", "live_monitor", *REQUIRED_EVIDENCE)


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str


@dataclass(frozen=True)
class LaunchSelection:
    strategy: str
    candidate_path: Path


class CommandRunner(Protocol):
    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        return CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
        )


class LaunchBlockedError(RuntimeError):
    """Raised before process execution when launch evidence is incomplete."""


def launch_dry_run(
    *,
    root_dir: Path,
    matrix_path: Path,
    candidate_package_path: Path,
    manifest_path: Path,
    env: Mapping[str, str],
    runner: CommandRunner,
    config_path: Path = Path("user_data/config.json"),
    log_path: Path = Path("user_data/logs/freqtrade.log"),
) -> CommandResult:
    root_dir = root_dir.resolve()
    resolved_matrix = _require_file_within_root(root_dir, matrix_path, "feature matrix")
    resolved_package = _require_file_within_root(root_dir, candidate_package_path, "research candidate package")
    package = _read_json_object(resolved_package, "research candidate package")
    receipt_path: Path | None = None
    if package.get("schema_version") == 3:
        try:
            receipt_path = dry_run_receipt_path(
                root_dir=root_dir,
                candidate_package_path=resolved_package,
            )
        except ValueError as exc:
            raise LaunchBlockedError(str(exc)) from exc
        receipt_path.unlink(missing_ok=True)
    selection = _validate_research_candidate_package(
        package,
        root_dir,
        matrix_path=resolved_matrix,
    )
    receipt_binding: dict[str, object] | None = None
    if selection.strategy == _SOTA_STRATEGY:
        try:
            receipt_path, receipt_binding = prepare_dry_run_launch_receipt(
                root_dir=root_dir,
                candidate_package_path=resolved_package,
                config_path=config_path,
                log_path=log_path,
            )
        except ValueError as exc:
            raise LaunchBlockedError(str(exc)) from exc

    resolved_manifest = _require_file_within_root(root_dir, manifest_path, "production manifest")
    manifest = _read_json_object(resolved_manifest, "production manifest")
    _require_manifest_hash(manifest, root_dir, resolved_package, "candidate package")
    child_env = dict(env)
    child_env["FREQTRADE__DRY_RUN"] = "true"
    _apply_candidate_environment(child_env, selection, root_dir)
    command_matrix_path = _command_path(
        root_dir,
        matrix_path,
        resolved_matrix,
        strategy=selection.strategy,
    )
    _apply_runtime_environment(child_env, command_matrix_path, selection.strategy)
    command = _launch_command(command_matrix_path, selection.strategy)
    launch_started_at = datetime.now(timezone.utc)
    result = _run_with_compose_override(command, root_dir=root_dir, env=child_env, runner=runner)
    launch_completed_at = datetime.now(timezone.utc)
    if result.returncode == 0 and receipt_path is not None and receipt_binding is not None:
        try:
            write_dry_run_launch_receipt(
                root_dir=root_dir,
                receipt_path=receipt_path,
                binding=receipt_binding,
                launch_started_at=launch_started_at,
                launch_completed_at=launch_completed_at,
            )
        except ValueError as exc:
            receipt_path.unlink(missing_ok=True)
            raise LaunchBlockedError(str(exc)) from exc
    return result


def launch_live(
    *,
    root_dir: Path,
    matrix_path: Path,
    candidate_package_path: Path,
    readiness_path: Path,
    manifest_path: Path,
    confirm_live: bool,
    env: Mapping[str, str],
    runner: CommandRunner,
    run_dir: Path | None = None,
) -> CommandResult:
    if not confirm_live:
        raise LaunchBlockedError("live launch requires explicit confirmation")
    if not env.get(_EXCHANGE_KEY, "").strip():
        raise LaunchBlockedError(f"live launch requires {_EXCHANGE_KEY}")
    if not env.get(_EXCHANGE_SECRET, "").strip():
        raise LaunchBlockedError(f"live launch requires {_EXCHANGE_SECRET}")

    root_dir = root_dir.resolve()
    resolved_matrix = _require_file_within_root(root_dir, matrix_path, "feature matrix")
    resolved_package = _require_file_within_root(root_dir, candidate_package_path, "candidate package")
    resolved_readiness = _require_file_within_root(root_dir, readiness_path, "live readiness")
    resolved_manifest = _require_file_within_root(root_dir, manifest_path, "production manifest")
    resolved_run_dir = run_dir.resolve() if run_dir is not None else resolved_manifest.parent
    _require_within(root_dir, resolved_run_dir, "production run")
    _require_within(resolved_run_dir, resolved_manifest, "production manifest")
    _require_within(resolved_run_dir, resolved_package, "candidate package")
    _require_within(resolved_run_dir, resolved_readiness, "live readiness")
    package = _read_json_object(resolved_package, "candidate package")
    selection = _validate_research_candidate_package(
        package,
        root_dir,
        matrix_path=resolved_matrix,
    )
    readiness = _read_json_object(resolved_readiness, "live readiness")
    _validate_live_readiness(
        readiness,
        root_dir=root_dir,
        candidate_path=resolved_package,
        run_dir=resolved_run_dir,
    )

    manifest = _read_json_object(resolved_manifest, "production manifest")
    _require_manifest_hash(manifest, root_dir, resolved_package, "candidate package")
    _require_manifest_hash(manifest, root_dir, resolved_readiness, "readiness report")

    child_env = dict(env)
    child_env["FREQTRADE__DRY_RUN"] = "false"
    _apply_candidate_environment(child_env, selection, root_dir)
    command_matrix_path = _command_path(
        root_dir,
        matrix_path,
        resolved_matrix,
        strategy=selection.strategy,
    )
    _apply_runtime_environment(child_env, command_matrix_path, selection.strategy)
    command = _launch_command(command_matrix_path, selection.strategy)
    return _run_with_compose_override(command, root_dir=root_dir, env=child_env, runner=runner)


def validate_live_readiness_evidence(
    *,
    root_dir: Path,
    run_dir: Path,
    candidate_package_path: Path,
    readiness_path: Path,
) -> tuple[Path, ...]:
    root_dir = root_dir.resolve()
    resolved_run_dir = run_dir.resolve()
    _require_within(root_dir, resolved_run_dir, "production run")
    resolved_package = _require_file_within_root(
        root_dir,
        candidate_package_path,
        "research candidate package",
    )
    _require_within(resolved_run_dir, resolved_package, "research candidate package")
    resolved_readiness = _require_file_within_root(root_dir, readiness_path, "live readiness")
    _require_within(resolved_run_dir, resolved_readiness, "live readiness")
    package = _read_json_object(resolved_package, "research candidate package")
    _validate_research_candidate_package(package, root_dir)
    readiness = _read_json_object(resolved_readiness, "live readiness")
    return _validate_live_readiness(
        readiness,
        root_dir=root_dir,
        candidate_path=resolved_package,
        run_dir=resolved_run_dir,
    )


def validate_candidate_package_for_launch(
    *,
    root_dir: Path,
    candidate_package_path: Path,
    matrix_path: Path,
) -> LaunchSelection:
    root_dir = root_dir.resolve()
    resolved_matrix = _require_file_within_root(root_dir, matrix_path, "feature matrix")
    resolved_package = _require_file_within_root(
        root_dir,
        candidate_package_path,
        "research candidate package",
    )
    package = _read_json_object(resolved_package, "research candidate package")
    return _validate_research_candidate_package(
        package,
        root_dir,
        matrix_path=resolved_matrix,
    )


def _launch_command(matrix_path: Path, strategy: str) -> tuple[str, ...]:
    return (
        "./ops/restart-strategy.sh",
        strategy,
        "--feature-matrix",
        str(matrix_path),
    )


def _command_path(
    root_dir: Path,
    supplied: Path,
    resolved: Path,
    *,
    strategy: str,
) -> Path:
    if strategy == _SOTA_STRATEGY:
        return resolved.relative_to(root_dir)
    if supplied.is_absolute():
        return resolved
    return resolved.relative_to(root_dir)


def _require_file_within_root(root_dir: Path, path: Path, label: str) -> Path:
    resolved = path.resolve() if path.is_absolute() else (root_dir / path).resolve()
    try:
        resolved.relative_to(root_dir)
    except ValueError as exc:
        raise LaunchBlockedError(f"{label} is outside root directory") from exc
    if not resolved.is_file():
        raise LaunchBlockedError(f"missing {label}")
    return resolved


def _require_within(parent: Path, path: Path, label: str) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:
        raise LaunchBlockedError(f"{label} is outside {parent.name or 'root'} directory") from exc


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise LaunchBlockedError(f"invalid or missing {label}") from exc
    if not isinstance(payload, dict):
        raise LaunchBlockedError(f"{label} must be a JSON object")
    if _contains_non_finite(payload):
        raise LaunchBlockedError(f"{label} contains non-finite metrics")
    return payload


def _contains_non_finite(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_non_finite(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_non_finite(item) for item in value)
    return False


def _require_manifest_hash(
    manifest: dict[str, object],
    root_dir: Path,
    artifact_path: Path,
    label: str,
) -> None:
    relative_path = artifact_path.relative_to(root_dir).as_posix()
    expected_artifact = _manifest_artifact(manifest, relative_path)
    if (
        expected_artifact is None
        or expected_artifact.get("sha256") != _sha256(artifact_path)
        or expected_artifact.get("size_bytes") != artifact_path.stat().st_size
    ):
        raise LaunchBlockedError(f"{label} hash does not match production manifest")


def _manifest_artifact(
    manifest: dict[str, object],
    relative_path: str,
) -> dict[str, object] | None:
    phases = manifest.get("phases")
    if not isinstance(phases, dict):
        return None
    for record in phases.values():
        if not isinstance(record, dict):
            continue
        outputs = record.get("outputs")
        if not isinstance(outputs, list):
            continue
        for artifact in outputs:
            if not isinstance(artifact, dict) or artifact.get("path") != relative_path:
                continue
            sha256 = artifact.get("sha256")
            size_bytes = artifact.get("size_bytes")
            if (
                artifact.get("exists") is True
                and isinstance(sha256, str)
                and isinstance(size_bytes, int)
                and not isinstance(size_bytes, bool)
            ):
                return artifact
            return None
    return None


def _validate_live_readiness(
    readiness: dict[str, object],
    *,
    root_dir: Path,
    candidate_path: Path,
    run_dir: Path | None,
) -> tuple[Path, ...]:
    if readiness.get("schema_version") != 2:
        raise LaunchBlockedError("live readiness schema_version is unsupported")
    if readiness.get("passed") is not True:
        raise LaunchBlockedError("live readiness did not pass")
    _require_complete_live_readiness(readiness)
    failed_reasons = readiness.get("failed_reasons")
    if failed_reasons != []:
        raise LaunchBlockedError("live readiness failed_reasons must be an empty list")
    artifacts = readiness.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(_LIVE_READINESS_CHECKS):
        raise LaunchBlockedError("live readiness artifact set is incomplete")
    verified: list[Path] = []
    for name in _LIVE_READINESS_CHECKS:
        artifact = artifacts[name]
        if not isinstance(artifact, dict):
            raise LaunchBlockedError(f"live readiness {name} artifact is malformed")
        path = _validate_readiness_artifact(
            name=name,
            artifact=artifact,
            root_dir=root_dir,
            run_dir=run_dir,
        )
        if name == "candidate_package" and path != candidate_path:
            raise LaunchBlockedError("candidate package path does not match live readiness")
        _validate_readiness_source(name, path, root_dir=root_dir)
        verified.append(path)
    package_payload = _read_json_object(candidate_path, "candidate package")
    if package_payload.get("schema_version") == 3:
        try:
            expected_selection, expected_sources = load_sota_runtime_binding(
                root_dir=root_dir,
                candidate_package_path=candidate_path,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise LaunchBlockedError(str(exc)) from exc
        artifacts_by_name = {
            name: path
            for name, path in zip(_LIVE_READINESS_CHECKS, verified, strict=True)
        }
        for evidence_name in ("dry_run", "risk"):
            payload = _read_json_object(
                artifacts_by_name[evidence_name],
                f"live readiness {evidence_name} artifact",
            )
            if evidence_name == "dry_run":
                reason = validate_schema_three_dry_run_evidence(
                    root_dir=root_dir,
                    candidate_package_path=candidate_path,
                    payload=payload,
                )
                if reason is not None:
                    raise LaunchBlockedError(f"live readiness dry_run receipt is invalid: {reason}")
            if payload.get("runtime_selection") != expected_selection:
                raise LaunchBlockedError(
                    f"live readiness {evidence_name} runtime selection does not match candidate package"
                )
            if evidence_name == "risk":
                risk_artifacts = payload.get("artifacts")
                if (
                    payload.get("strategy_name") != "Sota"
                    or not isinstance(risk_artifacts, dict)
                    or risk_artifacts.get("strategy") != expected_sources["sota"]
                    or risk_artifacts.get("strategy_runtime_sources") != expected_sources
                ):
                    raise LaunchBlockedError(
                        "live readiness risk strategy provenance does not match candidate package"
                    )
    return tuple(verified)


def _require_complete_live_readiness(readiness: dict[str, object]) -> None:
    checks = readiness.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(_LIVE_READINESS_CHECKS):
        raise LaunchBlockedError("live readiness is missing final promotion checks")
    failed = [
        name
        for name in _LIVE_READINESS_CHECKS
        if not isinstance(checks.get(name), dict)
        or set(checks[name]) != {"passed", "reason"}
        or checks[name].get("passed") is not True
        or checks[name].get("reason") is not None
    ]
    if failed:
        raise LaunchBlockedError(f"live readiness final promotion checks did not pass: {', '.join(failed)}")


def _validate_readiness_artifact(
    *,
    name: str,
    artifact: dict[str, object],
    root_dir: Path,
    run_dir: Path | None,
) -> Path:
    if set(artifact) != {"exists", "path", "sha256", "size_bytes"}:
        raise LaunchBlockedError(f"live readiness {name} artifact is malformed")
    path_text = artifact.get("path")
    expected_hash = artifact.get("sha256")
    expected_size = artifact.get("size_bytes")
    if (
        artifact.get("exists") is not True
        or not isinstance(path_text, str)
        or not path_text
        or not isinstance(expected_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size <= 0
    ):
        raise LaunchBlockedError(f"live readiness {name} artifact is incomplete")
    path = _require_file_within_root(root_dir, Path(path_text), f"live readiness {name} artifact")
    if run_dir is not None:
        _require_within(run_dir, path, f"live readiness {name} artifact")
    if path.stat().st_size != expected_size or _sha256(path) != expected_hash:
        raise LaunchBlockedError(f"live readiness {name} artifact does not match current file")
    return path


def _validate_readiness_source(name: str, path: Path, *, root_dir: Path) -> None:
    payload = _read_json_object(path, f"live readiness {name} artifact")
    reason = validate_readiness_source_payload(
        name,
        payload,
        min_dry_run_days=DEFAULT_MIN_DRY_RUN_DAYS,
    )
    if reason is not None:
        raise LaunchBlockedError(f"live readiness {name} source is invalid: {reason}")
    if name == "risk":
        _validate_risk_runtime_artifacts(payload, root_dir=root_dir)


def _validate_risk_runtime_artifacts(payload: dict[str, object], *, root_dir: Path) -> None:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise LaunchBlockedError("live readiness risk source has no runtime artifacts")
    for name in ("config", "strategy", "env_file"):
        artifact = artifacts.get(name)
        if not isinstance(artifact, dict):
            raise LaunchBlockedError(f"live readiness risk {name} artifact is malformed")
        expected_keys = {"exists", "path", "sha256", "size_bytes"}
        if name == "env_file":
            expected_keys.add("sensitive")
            if artifact.get("sensitive") is not True:
                raise LaunchBlockedError("live readiness risk env_file artifact is not sensitive")
        if set(artifact) != expected_keys:
            raise LaunchBlockedError(f"live readiness risk {name} artifact is malformed")
        path_text = artifact.get("path")
        expected_hash = artifact.get("sha256")
        expected_size = artifact.get("size_bytes")
        if (
            artifact.get("exists") is not True
            or not isinstance(path_text, str)
            or not path_text
            or not isinstance(expected_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size <= 0
        ):
            raise LaunchBlockedError(f"live readiness risk {name} artifact is incomplete")
        current = _require_file_within_root(root_dir, Path(path_text), f"risk {name}")
        if current.stat().st_size != expected_size or _sha256(current) != expected_hash:
            raise LaunchBlockedError(f"live readiness risk {name} artifact does not match current file")
    runtime_sources = artifacts.get("strategy_runtime_sources")
    if runtime_sources is not None:
        if not isinstance(runtime_sources, dict) or set(runtime_sources) != set(SOTA_RUNTIME_SOURCE_FILES):
            raise LaunchBlockedError("live readiness risk runtime strategy sources are malformed")
        for name in SOTA_RUNTIME_SOURCE_FILES:
            artifact = runtime_sources[name]
            if not isinstance(artifact, dict):
                raise LaunchBlockedError("live readiness risk runtime strategy source is malformed")
            _validate_package_artifact(root_dir, artifact, f"risk runtime strategy source {name}")


def _validate_research_candidate_package(
    package: dict[str, object],
    root_dir: Path,
    *,
    matrix_path: Path | None = None,
) -> LaunchSelection:
    schema_version = package.get("schema_version")
    if schema_version == 2:
        return LaunchSelection(
            strategy=_LEGACY_STRATEGY,
            candidate_path=_validate_legacy_research_candidate_package(package, root_dir),
        )
    if schema_version == 3:
        return _validate_sota_candidate_package(
            package,
            root_dir,
            matrix_path=matrix_path,
        )
    raise LaunchBlockedError("research candidate package schema_version is unsupported")


def _validate_legacy_research_candidate_package(
    package: dict[str, object],
    root_dir: Path,
) -> Path:
    reason = validate_readiness_source_payload("candidate_package", package)
    if reason is not None:
        raise LaunchBlockedError(f"research candidate package is invalid: {reason}")
    candidate_artifact = package.get("candidate_config_artifact")
    evolution_artifact = package.get("evolution_result_artifact")
    if not isinstance(candidate_artifact, dict) or not isinstance(evolution_artifact, dict):
        raise LaunchBlockedError("research candidate package artifact set is invalid")
    candidate_path = _validate_package_artifact(
        root_dir,
        candidate_artifact,
        "candidate config",
    )
    evolution_path = _validate_package_artifact(
        root_dir,
        evolution_artifact,
        "evolution result",
    )
    evolution = _read_json_object(evolution_path, "evolution result")
    selected = evolution.get("selected_candidate")
    selection = evolution.get("selection")
    candidate_id = package.get("candidate_id")
    if (
        evolution.get("schema_version") != 2
        or evolution.get("final_oos_evaluation_count") != 1
        or not isinstance(selected, dict)
        or not isinstance(selection, dict)
        or selected.get("candidate_id") != candidate_id
        or selected.get("candidate") != package.get("candidate")
        or selected.get("candidate_config_artifact") != candidate_artifact
        or selected.get("metrics") != package.get("metrics")
        or selection.get("selected_candidate_id") != candidate_id
        or selection != package.get("selection")
        or evolution.get("split_plan") != package.get("split_plan")
    ):
        raise LaunchBlockedError("evolution result does not match research candidate package")
    return candidate_path


def _validate_sota_candidate_package(
    package: dict[str, object],
    root_dir: Path,
    *,
    matrix_path: Path | None,
) -> LaunchSelection:
    reason = validate_readiness_source_payload("candidate_package", package)
    if reason is not None:
        raise LaunchBlockedError(f"research candidate package is invalid: {reason}")
    source_artifacts = package.get("source_artifacts")
    runtime_sources = package.get("runtime_strategy_sources")
    selection = package.get("selection")
    if (
        not isinstance(source_artifacts, dict)
        or not isinstance(runtime_sources, dict)
        or not isinstance(selection, dict)
    ):
        raise LaunchBlockedError("Sota candidate package source selection is malformed")

    validated_runtime_sources = {
        name: _validate_package_artifact(
            root_dir,
            _artifact_object(runtime_sources, name),
            f"runtime strategy source {name}",
        )
        for name in SOTA_RUNTIME_SOURCE_FILES
    }

    source_paths = {
        name: _validate_package_artifact(
            root_dir,
            _artifact_object(source_artifacts, name),
            name.replace("_", " "),
        )
        for name in (
            "candidate_acceptance",
            "hyperopt_walk_forward_plan",
            "hyperopt_walk_forward_result",
            "optimized_params",
            "rolling_candidate_package",
            "runtime_feature_matrix",
        )
    }
    resolved_matrix = matrix_path or source_paths["runtime_feature_matrix"]
    if resolved_matrix != source_paths["runtime_feature_matrix"]:
        raise LaunchBlockedError("supplied runtime feature matrix does not match candidate package")

    try:
        validated = validate_sota_rolling_sources(
            plan_path=source_paths["hyperopt_walk_forward_plan"],
            result_path=source_paths["hyperopt_walk_forward_result"],
            rolling_package_path=source_paths["rolling_candidate_package"],
            candidate_acceptance_path=source_paths["candidate_acceptance"],
            runtime_matrix_path=source_paths["runtime_feature_matrix"],
            root_dir=root_dir,
        )
    except ValueError as exc:
        raise LaunchBlockedError(str(exc)) from exc
    if runtime_sources != validated.runtime_strategy_sources:
        raise LaunchBlockedError("runtime strategy sources do not match rolling evidence")
    if any(path.name != SOTA_RUNTIME_SOURCE_FILES[name] for name, path in validated_runtime_sources.items()):
        raise LaunchBlockedError("runtime strategy source path is invalid")

    result = _read_json_object(source_paths["hyperopt_walk_forward_result"], "rolling result")
    rolling_package = _read_json_object(
        source_paths["rolling_candidate_package"],
        "rolling candidate package",
    )
    acceptance = _read_json_object(source_paths["candidate_acceptance"], "candidate acceptance")
    if (
        acceptance.get("passed") is not True
        or result.get("candidate_acceptance") != acceptance
        or rolling_package.get("passed") is not True
    ):
        raise LaunchBlockedError("Sota rolling candidate acceptance does not match package")
    linked_package_path = result.get("candidate_package_path")
    if not isinstance(linked_package_path, str) or _require_file_within_root(
        root_dir,
        Path(linked_package_path),
        "rolling candidate package",
    ) != source_paths["rolling_candidate_package"]:
        raise LaunchBlockedError("rolling result does not link to rolling candidate package")

    rolling_sources = rolling_package.get("source_artifacts")
    if not isinstance(rolling_sources, dict):
        raise LaunchBlockedError("rolling candidate package source artifacts are malformed")
    _require_matching_package_artifact(
        root_dir,
        rolling_sources.get("hyperopt_walk_forward_result"),
        source_paths["hyperopt_walk_forward_result"],
        "rolling result",
    )
    _require_matching_package_artifact(
        root_dir,
        rolling_sources.get("candidate_acceptance"),
        source_paths["candidate_acceptance"],
        "candidate acceptance",
    )

    splits = result.get("splits")
    package_splits = rolling_package.get("splits")
    if (
        not isinstance(splits, list)
        or len(splits) < 6
        or not isinstance(package_splits, list)
        or len(package_splits) != len(splits)
    ):
        raise LaunchBlockedError("Sota rolling candidate has fewer than 6 splits")
    latest_split = _latest_indexed_split(splits)
    latest_package_split = _indexed_package_split(package_splits, latest_split["index"])
    optimization_result = latest_split.get("optimization_result")
    latest_artifacts = latest_package_split.get("artifacts")
    if not isinstance(optimization_result, dict) or not isinstance(latest_artifacts, dict):
        raise LaunchBlockedError("latest rolling split artifacts are malformed")
    request = optimization_result.get("request")
    if not isinstance(request, dict) or request.get("strategy") != _SOTA_STRATEGY:
        raise LaunchBlockedError("latest rolling split strategy is not Sota")

    optimized_params = _artifact_object(optimization_result, "optimized_params")
    _require_matching_package_artifact(
        root_dir,
        optimized_params,
        source_paths["optimized_params"],
        "optimized params",
    )
    _require_matching_package_artifact(
        root_dir,
        latest_artifacts.get("optimized_params"),
        source_paths["optimized_params"],
        "latest rolling optimized params",
    )
    training_matrix = _artifact_object(latest_artifacts, "training_feature_matrix")
    training_matrix_path = _validate_package_artifact(
        root_dir,
        training_matrix,
        "latest training feature matrix",
    )
    if (
        training_matrix.get("hash_matches") is not True
        or training_matrix.get("recorded_sha256") != _sha256(training_matrix_path)
        or _sha256(training_matrix_path) != _sha256(source_paths["runtime_feature_matrix"])
    ):
        raise LaunchBlockedError("latest training feature matrix does not match runtime matrix")

    expected_selection = {
        "optimized_params_artifact": source_artifacts["optimized_params"],
        "split_index": latest_split["index"],
        "test_timerange": latest_split.get("test_timerange"),
        "train_timerange": latest_split.get("train_timerange"),
    }
    if (
        selection != expected_selection
        or latest_package_split.get("test_timerange") != latest_split.get("test_timerange")
        or latest_package_split.get("train_timerange") != latest_split.get("train_timerange")
    ):
        raise LaunchBlockedError("candidate package does not select the latest rolling split")
    evaluation = result.get("evaluation")
    aggregate = evaluation.get("aggregate") if isinstance(evaluation, dict) else None
    if not isinstance(aggregate, dict) or package.get("rolling_metrics") != aggregate:
        raise LaunchBlockedError("candidate package rolling metrics do not match rolling result")
    expected_candidate_id = f"sota-{_sha256(source_paths['optimized_params'])}"
    if package.get("candidate_id") != expected_candidate_id:
        raise LaunchBlockedError("candidate package candidate_id does not match optimized params")
    params_payload = _read_json_object(source_paths["optimized_params"], "Sota candidate params")
    try:
        validate_sota_candidate_parameters(params_payload)
    except ValueError as exc:
        raise LaunchBlockedError(str(exc)) from exc
    return LaunchSelection(
        strategy=_SOTA_STRATEGY,
        candidate_path=source_paths["optimized_params"],
    )


def _artifact_object(container: dict[str, object], name: str) -> dict[str, object]:
    artifact = container.get(name)
    if not isinstance(artifact, dict):
        raise LaunchBlockedError(f"research candidate package has malformed {name.replace('_', ' ')} artifact")
    return artifact


def _require_matching_package_artifact(
    root_dir: Path,
    artifact: object,
    expected_path: Path,
    label: str,
) -> None:
    if not isinstance(artifact, dict):
        raise LaunchBlockedError(f"{label} artifact is malformed")
    current_path = _validate_package_artifact(root_dir, artifact, label)
    if current_path != expected_path:
        raise LaunchBlockedError(f"{label} artifact path does not match candidate package")


def _latest_indexed_split(splits: list[object]) -> dict[str, object]:
    indexed: list[dict[str, object]] = []
    indexes: set[int] = set()
    for split in splits:
        index = split.get("index") if isinstance(split, dict) else None
        if not isinstance(index, int) or isinstance(index, bool) or index <= 0 or index in indexes:
            raise LaunchBlockedError("rolling result split indexes are malformed")
        indexes.add(index)
        indexed.append(split)
    return max(indexed, key=lambda split: int(split["index"]))


def _indexed_package_split(splits: list[object], index: object) -> dict[str, object]:
    matching = [
        split
        for split in splits
        if isinstance(split, dict) and split.get("index") == index
    ]
    if len(matching) != 1:
        raise LaunchBlockedError("latest rolling split is missing from rolling candidate package")
    return matching[0]


def _validate_package_artifact(
    root_dir: Path,
    artifact: dict[str, object],
    label: str,
) -> Path:
    path_text = artifact.get("path")
    expected_hash = artifact.get("sha256")
    expected_size = artifact.get("size_bytes")
    if (
        artifact.get("exists") is not True
        or not isinstance(path_text, str)
        or not isinstance(expected_hash, str)
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
    ):
        raise LaunchBlockedError(f"research candidate package has incomplete {label} artifact")
    path = _require_file_within_root(root_dir, Path(path_text), label)
    if path.stat().st_size != expected_size or _sha256(path) != expected_hash:
        raise LaunchBlockedError(f"{label} hash does not match candidate package")
    return path


def _apply_candidate_environment(
    child_env: dict[str, str],
    selection: LaunchSelection,
    root_dir: Path,
) -> None:
    relative_path = selection.candidate_path.relative_to(root_dir).as_posix()
    if selection.strategy == _SOTA_STRATEGY:
        _require_within(root_dir / "user_data", selection.candidate_path, "runtime Sota candidate")
        child_env["FREQTRADE_SOTA_CANDIDATE"] = relative_path
        child_env["FREQTRADE_EVOLUTION_CANDIDATE"] = ""
        return
    child_env["FREQTRADE_EVOLUTION_CANDIDATE"] = relative_path
    child_env["FREQTRADE_SOTA_CANDIDATE"] = ""


def _apply_runtime_environment(
    child_env: dict[str, str],
    matrix_path: Path,
    strategy: str,
) -> None:
    child_env[_NON_PERSISTENT_RUNTIME] = "true"
    child_env["FREQTRADE_STRATEGY"] = strategy
    child_env["FREQTRADE_FEATURE_MATRIX"] = str(matrix_path)


def _run_with_compose_override(
    command: tuple[str, ...],
    *,
    root_dir: Path,
    env: dict[str, str],
    runner: CommandRunner,
) -> CommandResult:
    override_text = """services:
  freqtrade:
    environment:
      FREQTRADE__DRY_RUN: ${FREQTRADE__DRY_RUN}
      FREQTRADE__EXCHANGE__KEY: ${FREQTRADE__EXCHANGE__KEY:-}
      FREQTRADE__EXCHANGE__SECRET: ${FREQTRADE__EXCHANGE__SECRET:-}
      FREQTRADE_EVOLUTION_CANDIDATE: ${FREQTRADE_EVOLUTION_CANDIDATE:-}
      FREQTRADE_FEATURE_MATRIX: ${FREQTRADE_FEATURE_MATRIX}
      FREQTRADE_SOTA_CANDIDATE: ${FREQTRADE_SOTA_CANDIDATE:-}
      FREQTRADE_STRATEGY: ${FREQTRADE_STRATEGY}
"""
    descriptor, temporary_name = tempfile.mkstemp(prefix="btc-quant-compose-", suffix=".yaml")
    override_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(override_text)
            handle.flush()
            os.fsync(handle.fileno())
        base_compose = env.get("COMPOSE_FILE", str(root_dir / "docker-compose.yml"))
        child_env = dict(env)
        child_env["COMPOSE_FILE"] = f"{base_compose}{os.pathsep}{override_path}"
        return runner.run(command, cwd=root_dir, env=child_env)
    finally:
        override_path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
