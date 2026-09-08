# Migrated verbatim from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def write_candidate_package(*, result_path: Path, root_dir: Path) -> Path:
    result = json.loads(result_path.read_text())
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

    package = {
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
    package_path = result_path.with_name("candidate_package.json")
    package_path.write_text(json.dumps(package, indent=2, sort_keys=True) + "\n")
    return package_path


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
    oos_run_dir_text = (
        optimization_result.get("out_of_sample_run_dir")
        if isinstance(optimization_result, dict)
        else None
    )
    oos_run_dir = Path(oos_run_dir_text) if isinstance(oos_run_dir_text, str) else root_dir / "missing-oos-run"
    manifest = _read_json(oos_run_dir / "manifest.json")

    return {
        "artifacts": {
            "optimized_params": _file_artifact(
                root_dir=root_dir,
                path=_artifact_path(optimized_params, root_dir=root_dir),
            ),
            "readiness": _file_artifact(root_dir=root_dir, path=oos_run_dir / "readiness.json"),
            "regime": _file_artifact(root_dir=root_dir, path=oos_run_dir / "regime.json"),
            "robustness": _file_artifact(root_dir=root_dir, path=oos_run_dir / "robustness.json"),
            "summary": _file_artifact(root_dir=root_dir, path=oos_run_dir / "summary.json"),
        },
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
        if not _artifact_complete(artifacts["optimized_params"]):
            failed_reasons.append(f"{label} missing optimized_params")
        if split_package["oos_manifest_status"] is None:
            failed_reasons.append(f"{label} missing manifest.json")
        if split_package["oos_manifest_status"] != "completed":
            failed_reasons.append(f"{label} manifest status is not completed")
        for artifact_name in ("summary", "robustness", "regime", "readiness"):
            if not _artifact_complete(artifacts[artifact_name]):
                failed_reasons.append(f"{label} missing {artifact_name}")
    return failed_reasons


def _artifact_complete(artifact: dict[str, Any]) -> bool:
    return artifact.get("exists") is True and bool(artifact.get("sha256"))


def _artifact_path(artifact: object, *, root_dir: Path) -> Path:
    if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
        path = Path(artifact["path"])
        return path if path.is_absolute() else root_dir / path
    return root_dir / "missing-optimized-params"


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

