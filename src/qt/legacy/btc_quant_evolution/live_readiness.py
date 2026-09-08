# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.strategy_provenance import SOTA_RUNTIME_SOURCE_FILES
from qt.legacy.btc_quant_evolution.runtime_evidence import load_sota_runtime_binding
from qt.legacy.btc_quant_evolution.dry_run_evidence import validate_schema_three_dry_run_evidence


REQUIRED_EVIDENCE = (
    "backup_restore",
    "dry_run",
    "healthcheck",
    "liquidity",
    "preflight",
    "reconciliation",
    "risk",
)
DEFAULT_MIN_DRY_RUN_DAYS = 21
CANDIDATE_PACKAGE_SCHEMA_VERSION = 2
SOTA_CANDIDATE_PACKAGE_SCHEMA_VERSION = 3


def write_live_readiness_report(
    *,
    output_dir: Path,
    root_dir: Path,
    candidate_package_path: Path,
    live_monitor_path: Path,
    evidence_paths: dict[str, Path],
    min_dry_run_days: int = DEFAULT_MIN_DRY_RUN_DAYS,
) -> Path:
    artifacts = {
        "candidate_package": _file_artifact(root_dir=root_dir, path=candidate_package_path),
        "live_monitor": _file_artifact(root_dir=root_dir, path=live_monitor_path),
    }
    for evidence_name in REQUIRED_EVIDENCE:
        artifacts[evidence_name] = _file_artifact(
            root_dir=root_dir,
            path=evidence_paths.get(evidence_name, root_dir / f"missing-{evidence_name}.json"),
        )

    checks = {
        "candidate_package": _candidate_package_check(candidate_package_path),
        "live_monitor": _live_monitor_check(live_monitor_path),
    }
    for evidence_name in REQUIRED_EVIDENCE:
        if evidence_name == "dry_run":
            checks[evidence_name] = _dry_run_check(
                path=evidence_paths.get(evidence_name),
                min_dry_run_days=min_dry_run_days,
            )
        else:
            checks[evidence_name] = _evidence_check(
                evidence_name=evidence_name,
                path=evidence_paths.get(evidence_name),
            )

    candidate_payload = _read_json(candidate_package_path)
    if isinstance(candidate_payload, dict) and candidate_payload.get("schema_version") == 3:
        try:
            expected_selection, expected_sources = load_sota_runtime_binding(
                root_dir=root_dir,
                candidate_package_path=candidate_package_path,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            checks["candidate_package"] = _failed(str(exc))
        else:
            for evidence_name in ("dry_run", "risk"):
                if checks[evidence_name]["passed"] is not True:
                    continue
                evidence_payload = _read_json(evidence_paths[evidence_name])
                reason = (
                    validate_schema_three_dry_run_evidence(
                        root_dir=root_dir,
                        candidate_package_path=candidate_package_path,
                        payload=evidence_payload,
                    )
                    if evidence_name == "dry_run" and isinstance(evidence_payload, dict)
                    else _runtime_link_reason(
                        evidence_name=evidence_name,
                        payload=evidence_payload,
                        expected_selection=expected_selection,
                        expected_sources=expected_sources,
                    )
                )
                if reason is not None:
                    checks[evidence_name] = _failed(reason)

    for name, artifact in artifacts.items():
        if checks[name]["passed"] is not True:
            continue
        artifact_reason = _artifact_integrity_reason(artifact)
        if artifact_reason is not None:
            checks[name] = _failed(artifact_reason)

    failed_reasons = [
        f"{name}: {check['reason']}"
        for name, check in checks.items()
        if check["reason"] is not None
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "artifacts": artifacts,
        "checks": checks,
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
        "schema_version": 2,
    }
    report_path = output_dir / "live_readiness.json"
    _write_json_atomic(report_path, report)
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate final live-promotion readiness evidence.")
    parser.add_argument("--candidate-package", type=Path, required=True)
    parser.add_argument("--live-monitor", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--healthcheck", type=Path, required=True)
    parser.add_argument("--liquidity", type=Path, required=True)
    parser.add_argument("--backup-restore", type=Path, required=True)
    parser.add_argument("--dry-run", type=Path, required=True)
    parser.add_argument("--reconciliation", type=Path, required=True)
    parser.add_argument("--risk", type=Path, required=True)
    parser.add_argument("--min-dry-run-days", type=int, default=DEFAULT_MIN_DRY_RUN_DAYS)
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/live-readiness"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    report_path = write_live_readiness_report(
        output_dir=args.output_dir,
        root_dir=args.root_dir.resolve(),
        candidate_package_path=args.candidate_package,
        live_monitor_path=args.live_monitor,
        evidence_paths={
            "backup_restore": args.backup_restore,
            "dry_run": args.dry_run,
            "healthcheck": args.healthcheck,
            "liquidity": args.liquidity,
            "preflight": args.preflight,
            "reconciliation": args.reconciliation,
            "risk": args.risk,
        },
        min_dry_run_days=args.min_dry_run_days,
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] is True else 1


def _candidate_package_check(path: Path) -> dict[str, bool | str | None]:
    payload = _read_json(path)
    if payload is None:
        return _failed("missing candidate package")
    reason = validate_readiness_source_payload("candidate_package", payload)
    if reason is None:
        return _passed()
    return _failed(reason)


def _live_monitor_check(path: Path) -> dict[str, bool | str | None]:
    payload = _read_json(path)
    if payload is None:
        return _failed("missing live monitor")
    reason = validate_readiness_source_payload("live_monitor", payload)
    if reason is None:
        return _passed()
    return _failed(reason)


def _evidence_check(*, evidence_name: str, path: Path | None) -> dict[str, bool | str | None]:
    if path is None:
        return _failed(f"missing {evidence_name} evidence")
    payload = _read_json(path)
    if payload is None:
        return _failed(f"missing {evidence_name} evidence")
    reason = validate_readiness_source_payload(evidence_name, payload)
    if reason is None:
        return _passed()
    return _failed(reason)


def _dry_run_check(*, path: Path | None, min_dry_run_days: int) -> dict[str, bool | str | None]:
    if path is None:
        return _failed("missing dry_run evidence")
    payload = _read_json(path)
    if payload is None:
        return _failed("missing dry_run evidence")
    reason = validate_readiness_source_payload(
        "dry_run",
        payload,
        min_dry_run_days=min_dry_run_days,
    )
    return _passed() if reason is None else _failed(reason)


def validate_readiness_source_payload(
    evidence_name: str,
    payload: dict[str, Any],
    *,
    min_dry_run_days: int = DEFAULT_MIN_DRY_RUN_DAYS,
) -> str | None:
    """Validate the stable shape emitted by each final operational evidence writer."""
    if evidence_name == "candidate_package":
        return _candidate_package_shape_reason(payload)
    if evidence_name == "live_monitor":
        return _live_monitor_shape_reason(payload)

    failed_reason = _passing_evidence_reason(payload, evidence_name)
    if failed_reason is not None:
        return failed_reason
    validators = {
        "backup_restore": _backup_restore_shape_reason,
        "dry_run": lambda value: _dry_run_shape_reason(value, min_dry_run_days),
        "healthcheck": lambda value: _command_shape_reason(value, "healthcheck"),
        "liquidity": _liquidity_shape_reason,
        "preflight": lambda value: _command_shape_reason(value, "preflight"),
        "reconciliation": _reconciliation_shape_reason,
        "risk": _risk_shape_reason,
    }
    validator = validators.get(evidence_name)
    if validator is None:
        return f"unsupported readiness evidence: {evidence_name}"
    return validator(payload)


def _candidate_package_shape_reason(payload: dict[str, Any]) -> str | None:
    if payload.get("schema_version") == SOTA_CANDIDATE_PACKAGE_SCHEMA_VERSION:
        return _sota_candidate_package_shape_reason(payload)
    if payload.get("passed") is not True:
        return _reason_from_failed_reasons(payload, "candidate package did not pass")
    if payload.get("schema_version") != CANDIDATE_PACKAGE_SCHEMA_VERSION:
        return "candidate package schema_version is unsupported"
    if payload.get("stage") != "research-qualified":
        return "candidate package stage is not research-qualified"
    candidate_id = payload.get("candidate_id")
    selection = payload.get("selection")
    qualification = payload.get("qualification")
    split_plan = payload.get("split_plan")
    if not isinstance(candidate_id, str) or not candidate_id:
        return "candidate package candidate_id is invalid"
    if not isinstance(payload.get("candidate"), dict) or not isinstance(payload.get("metrics"), dict):
        return "candidate package candidate or metrics shape is invalid"
    if not isinstance(selection, dict) or selection.get("selected_candidate_id") != candidate_id:
        return "candidate package selection does not match candidate_id"
    if (
        not isinstance(qualification, dict)
        or qualification.get("passed") is not True
        or qualification.get("stage") != "research-qualified"
    ):
        return "candidate package qualification is not research-qualified"
    if not _valid_split_plan(split_plan):
        return "candidate package split plan is invalid"
    for name in ("candidate_config_artifact", "evolution_result_artifact"):
        if not _valid_artifact_shape(payload.get(name)):
            return f"candidate package {name} is invalid"
    return None


def _sota_candidate_package_shape_reason(payload: dict[str, Any]) -> str | None:
    if payload.get("passed") is not True:
        return _reason_from_failed_reasons(payload, "candidate package did not pass")
    if payload.get("strategy") != "Sota":
        return "Sota candidate package strategy is invalid"
    if payload.get("qualification_engine") != "rolling_hyperopt_walk_forward":
        return "Sota candidate package qualification_engine is invalid"
    if payload.get("stage") != "research-qualified":
        return "candidate package stage is not research-qualified"
    candidate_id = payload.get("candidate_id")
    if not isinstance(candidate_id, str) or re.fullmatch(r"sota-[0-9a-f]{64}", candidate_id) is None:
        return "candidate package candidate_id is invalid"
    qualification = payload.get("qualification")
    if (
        not isinstance(qualification, dict)
        or qualification.get("passed") is not True
        or qualification.get("failed_reasons") != []
        or qualification.get("stage") != "research-qualified"
    ):
        return "candidate package qualification is not research-qualified"
    if not isinstance(payload.get("rolling_metrics"), dict):
        return "Sota candidate package rolling metrics are invalid"
    source_artifacts = payload.get("source_artifacts")
    expected_sources = {
        "candidate_acceptance",
        "hyperopt_walk_forward_plan",
        "hyperopt_walk_forward_result",
        "optimized_params",
        "rolling_candidate_package",
        "runtime_feature_matrix",
    }
    if not isinstance(source_artifacts, dict) or set(source_artifacts) != expected_sources:
        return "Sota candidate package source artifact set is invalid"
    if any(not _valid_artifact_shape(source_artifacts[name]) for name in expected_sources):
        return "Sota candidate package source artifact is invalid"
    runtime_sources = payload.get("runtime_strategy_sources")
    if not isinstance(runtime_sources, dict) or set(runtime_sources) != set(SOTA_RUNTIME_SOURCE_FILES):
        return "Sota candidate package runtime strategy source set is invalid"
    if any(not _valid_artifact_shape(runtime_sources[name]) for name in SOTA_RUNTIME_SOURCE_FILES):
        return "Sota candidate package runtime strategy source is invalid"
    selection = payload.get("selection")
    if (
        not isinstance(selection, dict)
        or set(selection)
        != {"optimized_params_artifact", "split_index", "test_timerange", "train_timerange"}
        or not _is_positive_int(selection.get("split_index"))
        or any(
            not isinstance(selection.get(name), str) or not selection[name]
            for name in ("test_timerange", "train_timerange")
        )
        or not _valid_artifact_shape(selection.get("optimized_params_artifact"))
        or selection.get("optimized_params_artifact") != source_artifacts["optimized_params"]
    ):
        return "Sota candidate package selection is invalid"
    return None


def _live_monitor_shape_reason(payload: dict[str, Any]) -> str | None:
    result = payload.get("result")
    if not isinstance(result, dict):
        return "missing result in live monitor"
    if result.get("passed") is not True:
        return _reason_from_failed_reasons(result, "live monitor did not pass")
    policy = payload.get("monitor_policy")
    required_top_level = ("completed_at", "exchange", "pair", "samples_path", "started_at")
    if any(not isinstance(payload.get(key), str) or not payload[key] for key in required_top_level):
        return "live monitor source shape is invalid"
    if not isinstance(policy, dict) or not isinstance(payload.get("snapshot_policy"), dict):
        return "live monitor source shape is invalid"
    sample_count = result.get("sample_count")
    passed_samples = result.get("passed_samples")
    failed_samples = result.get("failed_samples")
    pass_ratio = result.get("pass_ratio")
    min_samples = policy.get("min_samples")
    min_pass_ratio = policy.get("min_pass_ratio")
    if (
        not _is_positive_int(sample_count)
        or not isinstance(passed_samples, int)
        or isinstance(passed_samples, bool)
        or not isinstance(failed_samples, int)
        or isinstance(failed_samples, bool)
        or passed_samples + failed_samples != sample_count
        or not _is_number(pass_ratio)
        or not _is_positive_int(min_samples)
        or sample_count < min_samples
        or not _is_number(min_pass_ratio)
        or pass_ratio < min_pass_ratio
        or result.get("failed_reasons") != []
    ):
        return "live monitor source shape is invalid"
    return None


def _backup_restore_shape_reason(payload: dict[str, Any]) -> str | None:
    archive = payload.get("archive")
    contents = payload.get("contents")
    expected_contents = {"config", "strategies_archive", "strategy_file", "trades_db"}
    if not _valid_artifact_shape(archive):
        return "backup_restore source shape is invalid"
    if not isinstance(contents, dict) or set(contents) != expected_contents:
        return "backup_restore source shape is invalid"
    if any(contents[name] is not True for name in expected_contents):
        return "backup_restore source contents are incomplete"
    return None


def _dry_run_shape_reason(payload: dict[str, Any], min_dry_run_days: int) -> str | None:
    duration_days = payload.get("duration_days")
    recorded_minimum = payload.get("min_duration_days")
    if not _is_number(duration_days):
        return "missing dry_run duration_days"
    if duration_days < min_dry_run_days:
        return f"dry_run duration below {min_dry_run_days} days"
    if (
        not _is_number(recorded_minimum)
        or duration_days < recorded_minimum
        or any(
            not isinstance(payload.get(key), str) or not payload[key]
            for key in ("config_path", "first_log_at", "last_log_at", "log_path")
        )
    ):
        return "dry_run source shape is invalid"
    return None


def _command_shape_reason(payload: dict[str, Any], name: str) -> str | None:
    expected_command = {
        "healthcheck": ["./ops/healthcheck.sh"],
        "preflight": ["./ops/preflight.sh", "--exchange-check"],
    }[name]
    if (
        payload.get("name") != name
        or payload.get("command") != expected_command
        or payload.get("exit_code") != 0
        or any(
            not isinstance(payload.get(key), str)
            for key in ("completed_at", "cwd", "output_tail", "started_at")
        )
    ):
        return f"{name} source shape is invalid"
    return None


def _liquidity_shape_reason(payload: dict[str, Any]) -> str | None:
    if payload.get("live_market_result_passed") is not True:
        return "liquidity live market result did not pass"
    if not isinstance(payload.get("live_market_path"), str) or not isinstance(payload.get("policy"), dict):
        return "liquidity source shape is invalid"
    numeric_fields = (
        "available_buy_notional",
        "available_sell_notional",
        "buy_depth_multiple",
        "buy_impact_bps",
        "order_notional",
        "sell_depth_multiple",
        "sell_impact_bps",
    )
    if any(not _is_number(payload.get(field)) for field in numeric_fields):
        return "liquidity source shape is invalid"
    return None


def _reconciliation_shape_reason(payload: dict[str, Any]) -> str | None:
    string_fields = ("asset", "balance_path", "open_trades_path", "pair")
    numeric_fields = (
        "difference",
        "exchange_amount",
        "exchange_free_amount",
        "exchange_used_amount",
        "expected_amount",
        "open_trade_count",
        "tolerance",
    )
    if any(not isinstance(payload.get(field), str) or not payload[field] for field in string_fields):
        return "reconciliation source shape is invalid"
    if any(not _is_number(payload.get(field)) for field in numeric_fields):
        return "reconciliation source shape is invalid"
    return None


def _risk_shape_reason(payload: dict[str, Any]) -> str | None:
    checks = payload.get("checks")
    artifacts = payload.get("artifacts")
    if (
        not isinstance(checks, dict)
        or not {"config", "env", "strategy"}.issubset(checks)
        or any(
            not isinstance(checks[name], dict)
            or checks[name].get("passed") is not True
            or checks[name].get("reason") is not None
            for name in ("config", "env", "strategy")
        )
        or not isinstance(artifacts, dict)
        or not {"config", "env_file", "strategy"}.issubset(artifacts)
        or not _valid_artifact_shape(artifacts.get("config"))
        or not _valid_artifact_shape(artifacts.get("strategy"))
        or not _valid_sensitive_artifact_shape(artifacts.get("env_file"))
        or any(
            not isinstance(payload.get(name), dict)
            for name in ("env_risk", "policy", "risk", "strategy_risk")
        )
    ):
        return "risk source shape is invalid"
    return None


def _runtime_link_reason(
    *,
    evidence_name: str,
    payload: dict[str, Any] | None,
    expected_selection: dict[str, Any],
    expected_sources: dict[str, dict[str, Any]],
) -> str | None:
    if not isinstance(payload, dict) or payload.get("runtime_selection") != expected_selection:
        return "runtime selection does not match candidate package"
    if evidence_name == "risk":
        artifacts = payload.get("artifacts")
        if (
            payload.get("strategy_name") != "Sota"
            or not isinstance(artifacts, dict)
            or artifacts.get("strategy") != expected_sources["sota"]
            or artifacts.get("strategy_runtime_sources") != expected_sources
        ):
            return "risk strategy provenance does not match candidate package"
    return None


def _passing_evidence_reason(payload: dict[str, Any], evidence_name: str) -> str | None:
    if payload.get("passed") is not True:
        return _reason_from_failed_reasons(payload, f"{evidence_name} evidence did not pass")
    if payload.get("failed_reasons") != []:
        return f"{evidence_name} failed_reasons must be an empty list"
    return None


def _valid_artifact_shape(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    sha256 = value.get("sha256")
    size_bytes = value.get("size_bytes")
    return (
        value.get("exists") is True
        and isinstance(value.get("path"), str)
        and bool(value["path"])
        and isinstance(sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", sha256) is not None
        and isinstance(size_bytes, int)
        and not isinstance(size_bytes, bool)
        and size_bytes > 0
    )


def _artifact_integrity_reason(value: object) -> str | None:
    if not _valid_artifact_shape(value):
        return "artifact is missing, empty, or outside the root directory"
    return None


def _valid_split_plan(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 3:
        return False
    expected = (
        ("train", "candidate_selection"),
        ("validate", "candidate_selection"),
        ("final_oos", "promotion_gate_only"),
    )
    for item, (name, purpose) in zip(value, expected, strict=True):
        if (
            not isinstance(item, dict)
            or item.get("name") != name
            or item.get("purpose") != purpose
            or any(not isinstance(item.get(key), str) or not item[key] for key in ("start", "end", "timerange"))
        ):
            return False
    return True


def _is_number(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_sensitive_artifact_shape(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("exists") is True
        and isinstance(value.get("path"), str)
        and bool(value["path"])
        and value.get("sensitive") is True
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
        and isinstance(value.get("size_bytes"), int)
        and not isinstance(value["size_bytes"], bool)
        and value["size_bytes"] > 0
    )


def _reason_from_failed_reasons(payload: dict[str, Any], fallback: str) -> str:
    failed_reasons = payload.get("failed_reasons")
    if isinstance(failed_reasons, list) and failed_reasons:
        return "; ".join(str(reason) for reason in failed_reasons)
    return fallback


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else None


def _file_artifact(*, root_dir: Path, path: Path) -> dict[str, Any]:
    root_dir = root_dir.resolve()
    path = path.resolve()
    try:
        relative_path = path.relative_to(root_dir)
    except ValueError:
        return {
            "exists": False,
            "path": path.as_posix(),
            "sha256": None,
            "size_bytes": None,
        }
    if not path.is_file():
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


def _passed() -> dict[str, bool | str | None]:
    return {"passed": True, "reason": None}


def _failed(reason: str) -> dict[str, bool | str | None]:
    return {"passed": False, "reason": reason}


def _write_json_atomic(path: Path, payload: object) -> None:
    temporary_path = path.with_name(f"{path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())

