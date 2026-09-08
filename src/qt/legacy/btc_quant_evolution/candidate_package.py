# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.candidate_acceptance import evaluate_candidate_acceptance
from qt.legacy.btc_quant_evolution.readiness import REQUIRED_ARTIFACTS, evaluate_json_gate
from qt.legacy.btc_quant_evolution.strategy_provenance import SOTA_RUNTIME_SOURCE_FILES
from qt.legacy.btc_quant_evolution.research_pipeline import FEATURE_MATRIX_STRATEGIES
from qt.legacy.btc_quant_evolution.walk_forward import evaluate_walk_forward
from qt.legacy.btc_quant_evolution.freqtrade_strategies.sota_params import validate_sota_candidate_parameters


SOTA_CANDIDATE_SCHEMA_VERSION = 3
SOTA_MIN_ROLLING_SPLITS = 6
_OOS_READINESS_CHECKS = (
    "manifest_completed",
    "reproducible_artifacts",
    "data_quality",
    "ohlcv_integrity",
    "production_gate",
    "robustness_gate",
    "regime_gate",
)
_OOS_GATE_EVIDENCE = {
    "data_quality": ("data_quality", ("result",), "data_quality.json"),
    "ohlcv_integrity": ("ohlcv_integrity", ("result",), "ohlcv_integrity.json"),
    "production_gate": ("summary", ("production_gate",), "summary.json"),
    "robustness_gate": ("robustness", ("robustness_gate",), "robustness.json"),
    "regime_gate": ("regime", ("regime_gate",), "regime.json"),
}


@dataclass(frozen=True)
class ValidatedSotaRollingSources:
    aggregate: dict[str, Any]
    latest_split: dict[str, Any]
    optimized_params_path: Path
    runtime_strategy_sources: dict[str, dict[str, Any]]


def write_candidate_package(*, result_path: Path, root_dir: Path) -> Path:
    result = json.loads(result_path.read_text())
    package = build_candidate_package(result=result, result_path=result_path, root_dir=root_dir)
    package_path = result_path.with_name("candidate_package.json")
    package_path.write_text(json.dumps(package, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return package_path


def build_candidate_package(
    *,
    result: dict[str, Any],
    result_path: Path,
    root_dir: Path,
) -> dict[str, Any]:
    candidate_acceptance = result.get("candidate_acceptance")
    splits = result.get("splits")
    source_artifacts = {
        "candidate_acceptance": _file_artifact(
            root_dir=root_dir,
            path=result_path.with_name("candidate_acceptance.json"),
        ),
        "hyperopt_walk_forward_result": _file_artifact(root_dir=root_dir, path=result_path),
    }
    split_packages = _split_packages(splits, root_dir=root_dir)
    failed_reasons = _failed_reasons(
        candidate_acceptance,
        source_artifacts=source_artifacts,
        split_packages=split_packages,
        splits=splits,
    )

    return {
        "failed_reasons": failed_reasons,
        "git_revisions": sorted(
            {
                split_package["git_revision"]
                for split_package in split_packages
                if isinstance(split_package.get("git_revision"), str)
            }
        ),
        "passed": not failed_reasons,
        "source_artifacts": source_artifacts,
        "splits": split_packages,
    }


def write_sota_candidate_package(
    *,
    plan_path: Path,
    result_path: Path,
    rolling_package_path: Path,
    candidate_acceptance_path: Path,
    runtime_matrix_path: Path,
    output_path: Path,
    root_dir: Path,
) -> Path:
    root_dir = root_dir.resolve()
    resolved_plan = _require_file_within_root(root_dir, plan_path, "rolling plan artifact")
    resolved_result = _require_file_within_root(root_dir, result_path, "rolling result artifact")
    resolved_rolling_package = _require_file_within_root(
        root_dir,
        rolling_package_path,
        "rolling candidate package artifact",
    )
    resolved_acceptance = _require_file_within_root(
        root_dir,
        candidate_acceptance_path,
        "candidate acceptance artifact",
    )
    resolved_runtime_matrix = _require_file_within_root(
        root_dir,
        runtime_matrix_path,
        "runtime feature matrix",
    )
    resolved_output = _resolve_within_root(root_dir, output_path, "Sota candidate package output")

    validated = validate_sota_rolling_sources(
        plan_path=resolved_plan,
        result_path=resolved_result,
        rolling_package_path=resolved_rolling_package,
        candidate_acceptance_path=resolved_acceptance,
        runtime_matrix_path=resolved_runtime_matrix,
        root_dir=root_dir,
    )
    latest_split = validated.latest_split
    resolved_params = validated.optimized_params_path
    params_artifact = _file_artifact(root_dir=root_dir, path=resolved_params)
    package = {
        "candidate_id": f"sota-{params_artifact['sha256']}",
        "passed": True,
        "qualification": {
            "failed_reasons": [],
            "passed": True,
            "stage": "research-qualified",
        },
        "qualification_engine": "rolling_hyperopt_walk_forward",
        "rolling_metrics": validated.aggregate,
        "runtime_strategy_sources": validated.runtime_strategy_sources,
        "schema_version": SOTA_CANDIDATE_SCHEMA_VERSION,
        "selection": {
            "optimized_params_artifact": params_artifact,
            "split_index": latest_split["index"],
            "test_timerange": latest_split.get("test_timerange"),
            "train_timerange": latest_split.get("train_timerange"),
        },
        "source_artifacts": {
            "candidate_acceptance": _file_artifact(root_dir=root_dir, path=resolved_acceptance),
            "hyperopt_walk_forward_plan": _file_artifact(root_dir=root_dir, path=resolved_plan),
            "hyperopt_walk_forward_result": _file_artifact(root_dir=root_dir, path=resolved_result),
            "optimized_params": params_artifact,
            "rolling_candidate_package": _file_artifact(
                root_dir=root_dir,
                path=resolved_rolling_package,
            ),
            "runtime_feature_matrix": _file_artifact(
                root_dir=root_dir,
                path=resolved_runtime_matrix,
            ),
        },
        "stage": "research-qualified",
        "strategy": "Sota",
    }
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(json.dumps(package, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return resolved_output


def validate_sota_rolling_sources(
    *,
    plan_path: Path,
    result_path: Path,
    rolling_package_path: Path,
    candidate_acceptance_path: Path,
    runtime_matrix_path: Path,
    root_dir: Path,
) -> ValidatedSotaRollingSources:
    root_dir = root_dir.resolve()
    resolved_plan = _require_file_within_root(root_dir, plan_path, "rolling plan artifact")
    resolved_result = _require_file_within_root(root_dir, result_path, "rolling result artifact")
    resolved_rolling_package = _require_file_within_root(
        root_dir, rolling_package_path, "rolling candidate package artifact"
    )
    resolved_acceptance = _require_file_within_root(
        root_dir, candidate_acceptance_path, "candidate acceptance artifact"
    )
    resolved_runtime_matrix = _require_file_within_user_data(
        root_dir, runtime_matrix_path, "runtime feature matrix"
    )
    plan = _read_json_object(resolved_plan, "rolling plan artifact")
    result = _read_json_object(resolved_result, "rolling result artifact")
    rolling_package = _read_json_object(
        resolved_rolling_package, "rolling candidate package artifact"
    )
    candidate_acceptance = _read_json_object(
        resolved_acceptance, "candidate acceptance artifact"
    )
    splits = result.get("splits")
    if not isinstance(splits, list) or len(splits) < SOTA_MIN_ROLLING_SPLITS:
        raise ValueError("Sota deployment requires an accepted rolling candidate with at least 6 splits")
    if not all(isinstance(split, dict) for split in splits):
        raise ValueError("rolling result splits are malformed")

    typed_splits = [split for split in splits if isinstance(split, dict)]
    _validate_rolling_plan_link(plan=plan, result_splits=typed_splits)
    package_splits = rolling_package.get("splits")
    if not isinstance(package_splits, list):
        raise ValueError("rolling candidate package splits are malformed")
    latest_split = _latest_split(splits)
    latest_params_path: Path | None = None
    latest_matrix_path: Path | None = None
    runtime_strategy_sources: dict[str, dict[str, Any]] | None = None
    for split in typed_splits:
        index = split.get("index")
        package_split = _package_split(package_splits, index)
        optimization_result = split.get("optimization_result")
        if not isinstance(optimization_result, dict):
            raise ValueError(f"rolling split {index} optimization result is malformed")
        request = optimization_result.get("request")
        if (
            not isinstance(request, dict)
            or request.get("strategy") != "Sota"
            or request.get("train_timerange") != split.get("train_timerange")
            or request.get("test_timerange") != split.get("test_timerange")
        ):
            raise ValueError(f"rolling split {index} request timeranges do not match split")
        params_path = _require_artifact_file(
            root_dir,
            optimization_result.get("optimized_params"),
            f"rolling split {index} optimized parameters artifact",
        )
        matrix_path = _require_artifact_file(
            root_dir,
            optimization_result.get("feature_matrix_artifact"),
            f"rolling split {index} training feature matrix artifact",
        )
        package_artifacts = package_split.get("artifacts")
        if not isinstance(package_artifacts, dict):
            raise ValueError(f"rolling candidate package split {index} artifacts are malformed")
        for name, artifact in package_artifacts.items():
            _require_artifact_file(
                root_dir,
                artifact,
                f"rolling candidate package split {index} {name} artifact",
            )
        _require_recorded_artifact(
            root_dir,
            package_artifacts.get("optimized_params"),
            params_path,
            f"rolling candidate package split {index} optimized parameters artifact",
        )
        _require_recorded_artifact(
            root_dir,
            package_artifacts.get("training_feature_matrix"),
            matrix_path,
            f"rolling candidate package split {index} training feature matrix artifact",
        )
        split_runtime_sources = _validate_oos_evidence(
            root_dir=root_dir,
            split=split,
            package_split=package_split,
            package_artifacts=package_artifacts,
            optimization_result=optimization_result,
        )
        if runtime_strategy_sources is None:
            runtime_strategy_sources = split_runtime_sources
        elif runtime_strategy_sources != split_runtime_sources:
            raise ValueError("Sota runtime strategy sources differ across rolling splits")
        if index == latest_split["index"]:
            latest_params_path = params_path
            latest_matrix_path = matrix_path

    if latest_params_path is None or latest_matrix_path is None or runtime_strategy_sources is None:
        raise ValueError("latest rolling split artifacts are malformed")
    _require_within_user_data(root_dir, latest_params_path, "runtime Sota candidate")
    validate_sota_candidate_parameters(
        _read_json_object(latest_params_path, "Sota candidate optimized parameters")
    )
    latest_package_split = _package_split(package_splits, latest_split["index"])
    latest_artifacts = latest_package_split.get("artifacts")
    training_matrix = (
        latest_artifacts.get("training_feature_matrix")
        if isinstance(latest_artifacts, dict)
        else None
    )
    package_matrix_path = _require_artifact_file(
        root_dir, training_matrix, "latest training feature matrix artifact"
    )
    expected_hash = _sha256(resolved_runtime_matrix)
    if _sha256(latest_matrix_path) != expected_hash or _sha256(package_matrix_path) != expected_hash:
        raise ValueError(
            "latest training feature matrix does not match runtime feature matrix artifact"
        )

    recomputed_evaluation = evaluate_walk_forward(typed_splits)
    if result.get("evaluation") != recomputed_evaluation or recomputed_evaluation.get("passed") is not True:
        raise ValueError("rolling result evaluation does not match its split evidence")
    try:
        recomputed_acceptance = evaluate_candidate_acceptance(result, root_dir=root_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("candidate acceptance cannot be recomputed from rolling result") from exc
    if (
        candidate_acceptance != recomputed_acceptance
        or result.get("candidate_acceptance") != recomputed_acceptance
        or recomputed_acceptance.get("passed") is not True
    ):
        raise ValueError("candidate acceptance does not match the rolling result")

    expected_rolling_package = build_candidate_package(
        result=result,
        result_path=resolved_result,
        root_dir=root_dir,
    )
    if rolling_package != expected_rolling_package or expected_rolling_package.get("passed") is not True:
        raise ValueError("rolling candidate package does not match the rolling result artifacts")
    linked_package_path = result.get("candidate_package_path")
    if not isinstance(linked_package_path, str) or _resolve_within_root(
        root_dir, Path(linked_package_path), "rolling candidate package artifact"
    ) != resolved_rolling_package:
        raise ValueError("rolling result does not link to the rolling candidate package artifact")

    aggregate = recomputed_evaluation.get("aggregate")
    if not isinstance(aggregate, dict):
        raise ValueError("rolling result aggregate metrics are malformed")
    return ValidatedSotaRollingSources(
        aggregate=aggregate,
        latest_split=latest_split,
        optimized_params_path=latest_params_path,
        runtime_strategy_sources=runtime_strategy_sources,
    )


def _validate_rolling_plan_link(
    *,
    plan: dict[str, Any],
    result_splits: list[dict[str, Any]],
) -> None:
    plan_request = plan.get("request")
    plan_splits = plan.get("splits")
    if not isinstance(plan_request, dict) or not isinstance(plan_splits, list):
        raise ValueError("rolling plan is malformed")
    if len(plan_splits) != len(result_splits):
        raise ValueError("rolling plan split count does not match rolling result")
    for plan_split, result_split in zip(plan_splits, result_splits, strict=True):
        if not isinstance(plan_split, dict):
            raise ValueError("rolling plan splits are malformed")
        identity = {
            key: plan_split.get(key)
            for key in ("index", "test_timerange", "train_timerange")
        }
        result_identity = {
            key: result_split.get(key)
            for key in ("index", "test_timerange", "train_timerange")
        }
        if identity != result_identity:
            raise ValueError("rolling plan split geometry does not match rolling result")
        optimization_result = result_split.get("optimization_result")
        optimization_request = (
            optimization_result.get("request")
            if isinstance(optimization_result, dict)
            else None
        )
        if not isinstance(optimization_request, dict):
            raise ValueError(f"rolling split {identity['index']} optimization request is malformed")
        index = identity["index"]
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("rolling plan split index is malformed")
        prefix = plan_request.get("run_id_prefix")
        expected_run_id = f"hwf-{index:03d}-{identity['test_timerange']}"
        if isinstance(prefix, str):
            expected_run_id = f"{prefix}-{expected_run_id}"
        expected_request = {
            "epochs": plan_request.get("epochs"),
            "exchange": plan_request.get("exchange"),
            "exchange_check": index == 1 and plan_request.get("skip_download") is not True,
            "feature_matrix": plan_request.get("feature_matrix"),
            "hyperopt_loss": plan_request.get("hyperopt_loss"),
            "job_workers": plan_request.get("job_workers"),
            "min_trades": plan_request.get("min_trades"),
            "pair": plan_request.get("pair"),
            "random_state": plan_request.get("random_state") + index - 1
            if isinstance(plan_request.get("random_state"), int)
            else None,
            "run_id": expected_run_id,
            "skip_download": plan_request.get("skip_download"),
            "spaces": plan_request.get("spaces"),
            "strategy": plan_request.get("strategy"),
            "test_timerange": identity["test_timerange"],
            "timeframe": plan_request.get("timeframe"),
            "train_timerange": identity["train_timerange"],
        }
        if optimization_request != expected_request:
            raise ValueError(
                f"rolling split {index} optimization request does not match rolling plan"
            )


def _latest_split(splits: list[object]) -> dict[str, Any]:
    indexed: list[dict[str, Any]] = []
    indexes: set[int] = set()
    for split in splits:
        index = split.get("index") if isinstance(split, dict) else None
        if not isinstance(index, int) or isinstance(index, bool) or index <= 0 or index in indexes:
            raise ValueError("rolling result split indexes are malformed")
        indexes.add(index)
        indexed.append(split)
    return max(indexed, key=lambda split: split["index"])


def _package_split(splits: list[object], index: int) -> dict[str, Any]:
    matching = [
        split
        for split in splits
        if isinstance(split, dict) and split.get("index") == index
    ]
    if len(matching) != 1:
        raise ValueError("latest rolling split is missing from the rolling candidate package")
    return matching[0]


def _validate_oos_evidence(
    *,
    root_dir: Path,
    split: dict[str, Any],
    package_split: dict[str, Any],
    package_artifacts: dict[str, Any],
    optimization_result: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    index = split["index"]
    evidence = {
        name: _read_json_object(
            _require_artifact_file(
                root_dir,
                package_artifacts.get(name),
                f"rolling split {index} {name} evidence",
            ),
            f"rolling split {index} {name} evidence",
        )
        for name in (
            "data_quality",
            "ohlcv_integrity",
            "summary",
            "robustness",
            "regime",
            "readiness",
        )
    }
    for name in ("summary", "robustness", "regime"):
        if split.get(name) != evidence[name]:
            raise ValueError(f"rolling split {index} {name} evidence does not match embedded result")

    oos_run_dir_text = optimization_result.get("out_of_sample_run_dir")
    if not isinstance(oos_run_dir_text, str):
        raise ValueError(f"rolling split {index} OOS run directory is malformed")
    oos_run_dir = _resolve_within_root(
        root_dir,
        Path(oos_run_dir_text),
        f"rolling split {index} OOS run directory",
    )
    if not oos_run_dir.is_dir():
        raise ValueError(f"rolling split {index} OOS run directory is missing")
    manifest = _read_json_object(
        _require_file_within_root(
            root_dir,
            oos_run_dir / "manifest.json",
            f"rolling split {index} manifest evidence",
        ),
        f"rolling split {index} manifest evidence",
    )
    runtime_sources = _validate_oos_manifest(
        root_dir=root_dir,
        index=index,
        manifest=manifest,
        package_split=package_split,
    )
    _validate_oos_readiness(
        index=index,
        readiness=evidence["readiness"],
        gate_evidence=evidence,
    )
    if optimization_result.get("passed") is not True:
        raise ValueError(f"rolling split {index} optimization result did not pass readiness")
    recorded_readiness = optimization_result.get("out_of_sample_readiness")
    if recorded_readiness is not None and recorded_readiness != evidence["readiness"]:
        raise ValueError(f"rolling split {index} readiness evidence does not match optimization result")
    return runtime_sources


def _validate_oos_manifest(
    *,
    root_dir: Path,
    index: int,
    manifest: dict[str, Any],
    package_split: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    if manifest.get("status") != "completed":
        raise ValueError(f"rolling split {index} manifest status is not completed")
    completed_at = manifest.get("completed_at")
    if not isinstance(completed_at, str) or not completed_at.strip():
        raise ValueError(f"rolling split {index} manifest completion timestamp is missing")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or any(name not in artifacts for name in REQUIRED_ARTIFACTS):
        raise ValueError(f"rolling split {index} manifest artifact evidence is incomplete")
    if package_split.get("manifest_artifacts") != artifacts:
        raise ValueError(f"rolling split {index} manifest evidence does not match rolling package")
    for name in REQUIRED_ARTIFACTS:
        try:
            _require_artifact_file(
                root_dir,
                artifacts[name],
                f"rolling split {index} manifest {name} artifact",
            )
        except ValueError as exc:
            raise ValueError(
                f"rolling split {index} manifest artifact evidence is incomplete: {name}"
            ) from exc
    runtime_sources = artifacts.get("strategy_runtime_sources")
    if not isinstance(runtime_sources, dict) or set(runtime_sources) != set(SOTA_RUNTIME_SOURCE_FILES):
        raise ValueError(
            f"rolling split {index} manifest Sota runtime strategy source evidence is incomplete"
        )
    for name in SOTA_RUNTIME_SOURCE_FILES:
        _require_artifact_file(
            root_dir,
            runtime_sources[name],
            f"rolling split {index} runtime strategy source {name}",
        )
    return runtime_sources


def _validate_oos_readiness(
    *,
    index: int,
    readiness: dict[str, Any],
    gate_evidence: dict[str, dict[str, Any]],
) -> None:
    checks = readiness.get("checks")
    if (
        set(readiness) != {"checks", "failed_reasons", "passed"}
        or not isinstance(checks, dict)
        or set(checks) != set(_OOS_READINESS_CHECKS)
        or not all(
            isinstance(checks.get(name), dict)
            and set(checks[name]) == {"passed", "reason"}
            for name in _OOS_READINESS_CHECKS
        )
    ):
        raise ValueError(f"rolling split {index} readiness evidence is malformed")

    for check_name, (evidence_name, key_path, source_name) in _OOS_GATE_EVIDENCE.items():
        expected = evaluate_json_gate(
            gate_evidence[evidence_name],
            key_path=key_path,
            source_name=source_name,
        )
        if checks[check_name] != expected:
            raise ValueError(f"rolling split {index} readiness {check_name} is inconsistent")

    failed_reasons = [
        checks[name]["reason"]
        for name in _OOS_READINESS_CHECKS
        if checks[name]["reason"] is not None
    ]
    expected_passed = not failed_reasons
    if (
        readiness.get("failed_reasons") != failed_reasons
        or readiness.get("passed") is not expected_passed
        or readiness.get("passed") is not True
        or any(checks[name] != {"passed": True, "reason": None} for name in _OOS_READINESS_CHECKS)
    ):
        raise ValueError(f"rolling split {index} readiness evidence did not pass")


def _require_artifact_file(root_dir: Path, artifact: object, label: str) -> Path:
    if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
        raise ValueError(f"{label} is malformed")
    path = _require_file_within_root(root_dir, Path(artifact["path"]), label)
    _require_recorded_artifact(root_dir, artifact, path, label)
    return path


def _require_recorded_artifact(
    root_dir: Path,
    artifact: object,
    path: Path,
    label: str,
) -> None:
    expected = _file_artifact(root_dir=root_dir, path=path)
    if (
        not isinstance(artifact, dict)
        or artifact.get("exists") is not True
        or artifact.get("path") != expected["path"]
        or artifact.get("sha256") != expected["sha256"]
        or artifact.get("size_bytes") != expected["size_bytes"]
    ):
        raise ValueError(f"{label} does not match its recorded hash")


def _require_file_within_root(root_dir: Path, path: Path, label: str) -> Path:
    resolved = _resolve_within_root(root_dir, path, label)
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise ValueError(f"{label} is missing or empty")
    return resolved


def _require_file_within_user_data(root_dir: Path, path: Path, label: str) -> Path:
    resolved = _require_file_within_root(root_dir, path, label)
    _require_within_user_data(root_dir, resolved, label)
    return resolved


def _require_within_user_data(root_dir: Path, path: Path, label: str) -> None:
    try:
        path.relative_to(root_dir / "user_data")
    except ValueError as exc:
        raise ValueError(f"{label} is outside user_data directory") from exc


def _resolve_within_root(root_dir: Path, path: Path, label: str) -> Path:
    resolved = path.resolve() if path.is_absolute() else (root_dir / path).resolve()
    try:
        resolved.relative_to(root_dir)
    except ValueError as exc:
        raise ValueError(f"{label} is outside root directory") from exc
    return resolved


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} is malformed")
    if _contains_non_finite(payload):
        raise ValueError(f"{label} contains non-finite metrics")
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


def _split_packages(splits: object, *, root_dir: Path) -> list[dict[str, Any]]:
    if not isinstance(splits, list):
        return []
    return [
        _split_package(split, root_dir=root_dir)
        for split in splits
        if isinstance(split, dict)
    ]


def _split_package(split: dict[str, Any], *, root_dir: Path) -> dict[str, Any]:
    index = split.get("index")
    optimization_result = split.get("optimization_result")
    optimized_params = (
        optimization_result.get("optimized_params")
        if isinstance(optimization_result, dict)
        else None
    )
    request = optimization_result.get("request") if isinstance(optimization_result, dict) else None
    strategy = request.get("strategy") if isinstance(request, dict) else None
    feature_matrix_required = strategy in FEATURE_MATRIX_STRATEGIES
    feature_matrix_artifact = (
        optimization_result.get("feature_matrix_artifact")
        if isinstance(optimization_result, dict)
        else None
    )
    oos_run_dir_text = (
        optimization_result.get("out_of_sample_run_dir")
        if isinstance(optimization_result, dict)
        else None
    )
    oos_run_dir = Path(oos_run_dir_text) if isinstance(oos_run_dir_text, str) else root_dir / "missing-oos-run"
    manifest = _read_json(oos_run_dir / "manifest.json")

    artifacts = {
        "data_quality": _file_artifact(
            root_dir=root_dir,
            path=oos_run_dir / "data_quality.json",
        ),
        "ohlcv_integrity": _file_artifact(
            root_dir=root_dir,
            path=oos_run_dir / "ohlcv_integrity.json",
        ),
        "optimized_params": _file_artifact(
            root_dir=root_dir,
            path=_artifact_path(optimized_params, root_dir=root_dir),
        ),
        "readiness": _file_artifact(root_dir=root_dir, path=oos_run_dir / "readiness.json"),
        "regime": _file_artifact(root_dir=root_dir, path=oos_run_dir / "regime.json"),
        "robustness": _file_artifact(root_dir=root_dir, path=oos_run_dir / "robustness.json"),
        "summary": _file_artifact(root_dir=root_dir, path=oos_run_dir / "summary.json"),
    }
    if feature_matrix_required or feature_matrix_artifact is not None:
        artifacts["training_feature_matrix"] = _recorded_file_artifact(
            root_dir=root_dir,
            recorded=feature_matrix_artifact,
            path=_artifact_path(
                feature_matrix_artifact,
                root_dir=root_dir,
                missing_name="missing-training-feature-matrix",
            ),
        )

    return {
        "artifacts": artifacts,
        "git_revision": manifest.get("git_revision") if isinstance(manifest, dict) else None,
        "index": index,
        "manifest_artifacts": manifest.get("artifacts") if isinstance(manifest, dict) else {},
        "oos_manifest_status": manifest.get("status") if isinstance(manifest, dict) else None,
        "test_timerange": split.get("test_timerange"),
        "train_timerange": split.get("train_timerange"),
    }


def _failed_reasons(
    candidate_acceptance: object,
    *,
    source_artifacts: dict[str, dict[str, Any]],
    split_packages: list[dict[str, Any]],
    splits: object,
) -> list[str]:
    failed_reasons = []
    if not _artifact_complete(source_artifacts["candidate_acceptance"]):
        failed_reasons.append("missing candidate_acceptance")
    if not isinstance(splits, list):
        failed_reasons.append("missing splits")
    if not isinstance(candidate_acceptance, dict) or candidate_acceptance.get("passed") is not True:
        failed_reasons.append("candidate_acceptance did not pass")

    for split_package in split_packages:
        index = split_package.get("index")
        label = f"split {index}"
        artifacts = split_package["artifacts"]
        training_feature_matrix = artifacts.get("training_feature_matrix")
        if training_feature_matrix is not None and not _artifact_complete(training_feature_matrix):
            failed_reasons.append(f"{label} missing training_feature_matrix")
        elif training_feature_matrix is not None and training_feature_matrix.get("hash_matches") is not True:
            failed_reasons.append(f"{label} training_feature_matrix hash mismatch")
        if not _artifact_complete(artifacts["optimized_params"]):
            failed_reasons.append(f"{label} missing optimized_params")
        if split_package["oos_manifest_status"] is None:
            failed_reasons.append(f"{label} missing manifest.json")
        if split_package["oos_manifest_status"] != "completed":
            failed_reasons.append(f"{label} manifest status is not completed")
        for artifact_name in (
            "data_quality",
            "ohlcv_integrity",
            "summary",
            "robustness",
            "regime",
            "readiness",
        ):
            if not _artifact_complete(artifacts[artifact_name]):
                failed_reasons.append(f"{label} missing {artifact_name}")
    return failed_reasons


def _artifact_complete(artifact: dict[str, Any]) -> bool:
    return artifact.get("exists") is True and bool(artifact.get("sha256"))


def _artifact_path(
    artifact: object,
    *,
    root_dir: Path,
    missing_name: str = "missing-optimized-params",
) -> Path:
    if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
        path = Path(artifact["path"])
        return path if path.is_absolute() else root_dir / path
    return root_dir / missing_name


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else None


def _file_artifact(*, root_dir: Path, path: Path) -> dict[str, Any]:
    try:
        relative_path = path.relative_to(root_dir)
    except ValueError:
        relative_path = path
    if not path.exists():
        return {
            "exists": False,
            "path": relative_path.as_posix(),
            "sha256": None,
            "size_bytes": None,
        }
    return {
        "exists": True,
        "path": relative_path.as_posix(),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _recorded_file_artifact(
    *,
    root_dir: Path,
    path: Path,
    recorded: object,
) -> dict[str, Any]:
    artifact = _file_artifact(root_dir=root_dir, path=path)
    recorded_sha256 = recorded.get("sha256") if isinstance(recorded, dict) else None
    artifact["recorded_sha256"] = recorded_sha256
    artifact["hash_matches"] = (
        artifact.get("exists") is True
        and isinstance(recorded_sha256, str)
        and recorded_sha256 == artifact.get("sha256")
    )
    return artifact


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
