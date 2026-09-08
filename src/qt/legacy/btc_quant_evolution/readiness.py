# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.strategy_provenance import SOTA_RUNTIME_SOURCE_FILES


REQUIRED_ARTIFACTS = ("config", "ohlcv_data", "strategy")


def write_readiness_report(*, run_dir: Path) -> Path:
    checks = {
        "manifest_completed": _manifest_completed(run_dir),
        "reproducible_artifacts": _reproducible_artifacts(run_dir),
        "data_quality": _json_gate(run_dir / "data_quality.json", ("result",)),
        "ohlcv_integrity": _json_gate(run_dir / "ohlcv_integrity.json", ("result",)),
        "production_gate": _json_gate(run_dir / "summary.json", ("production_gate",)),
        "robustness_gate": _json_gate(run_dir / "robustness.json", ("robustness_gate",)),
        "regime_gate": _json_gate(run_dir / "regime.json", ("regime_gate",)),
    }
    failed_reasons = [check["reason"] for check in checks.values() if check["reason"] is not None]
    report = {
        "checks": checks,
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
    }
    report_path = run_dir / "readiness.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report_path


def _manifest_completed(run_dir: Path) -> dict[str, bool | str | None]:
    manifest = _read_json(run_dir / "manifest.json")
    if manifest is None:
        return _failed("missing manifest.json")
    if manifest.get("status") != "completed":
        return _failed("manifest status is not completed")
    return _passed()


def _reproducible_artifacts(run_dir: Path) -> dict[str, bool | str | None]:
    manifest = _read_json(run_dir / "manifest.json")
    if manifest is None:
        return _failed("missing manifest.json")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return _failed("missing artifact fingerprints: config, ohlcv_data, strategy")

    missing = []
    for name in REQUIRED_ARTIFACTS:
        artifact = artifacts.get(name)
        if not isinstance(artifact, dict) or not artifact.get("exists") or not artifact.get("sha256"):
            missing.append(name)
    if missing:
        return _failed(f"missing artifact fingerprints: {', '.join(missing)}")
    request = manifest.get("request")
    if isinstance(request, dict) and request.get("strategy") == "Sota":
        runtime_sources = artifacts.get("strategy_runtime_sources")
        if not isinstance(runtime_sources, dict):
            return _failed("missing artifact fingerprints: strategy_runtime_sources")
        missing_sources = [
            name
            for name in SOTA_RUNTIME_SOURCE_FILES
            if not isinstance(runtime_sources.get(name), dict)
            or runtime_sources[name].get("exists") is not True
            or not runtime_sources[name].get("sha256")
        ]
        if missing_sources:
            return _failed(
                "missing artifact fingerprints: strategy_runtime_sources."
                + ", strategy_runtime_sources.".join(missing_sources)
            )
    return _passed()


def _json_gate(path: Path, key_path: tuple[str, ...]) -> dict[str, bool | str | None]:
    payload = _read_json(path)
    if payload is None:
        return _failed(f"missing {path.name}")

    return evaluate_json_gate(
        payload,
        key_path=key_path,
        source_name=path.name,
    )


def evaluate_json_gate(
    payload: dict[str, Any],
    *,
    key_path: tuple[str, ...],
    source_name: str,
) -> dict[str, bool | str | None]:
    """Evaluate an already parsed readiness gate using report-writer semantics."""

    gate: Any = payload
    for key in key_path:
        if not isinstance(gate, dict) or key not in gate:
            return _failed(f"missing {'.'.join(key_path)} in {source_name}")
        gate = gate[key]

    if not isinstance(gate, dict):
        return _failed(f"invalid {'.'.join(key_path)} in {source_name}")
    if gate.get("passed") is True:
        return _passed()

    failed_reasons = gate.get("failed_reasons")
    if isinstance(failed_reasons, list) and failed_reasons:
        return _failed("; ".join(str(reason) for reason in failed_reasons))
    return _failed(f"{'.'.join(key_path)} did not pass")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else None


def _passed() -> dict[str, bool | str | None]:
    return {"passed": True, "reason": None}


def _failed(reason: str) -> dict[str, bool | str | None]:
    return {"passed": False, "reason": reason}

