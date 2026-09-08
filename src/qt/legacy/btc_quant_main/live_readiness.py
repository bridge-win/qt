# Migrated verbatim from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


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
    }
    report_path = output_dir / "live_readiness.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
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
    if payload.get("passed") is True:
        return _passed()
    return _failed(_reason_from_failed_reasons(payload, "candidate package did not pass"))


def _live_monitor_check(path: Path) -> dict[str, bool | str | None]:
    payload = _read_json(path)
    if payload is None:
        return _failed("missing live monitor")
    result = payload.get("result")
    if isinstance(result, dict) and result.get("passed") is True:
        return _passed()
    if isinstance(result, dict):
        return _failed(_reason_from_failed_reasons(result, "live monitor did not pass"))
    return _failed("missing result in live monitor")


def _evidence_check(*, evidence_name: str, path: Path | None) -> dict[str, bool | str | None]:
    if path is None:
        return _failed(f"missing {evidence_name} evidence")
    payload = _read_json(path)
    if payload is None:
        return _failed(f"missing {evidence_name} evidence")
    if payload.get("passed") is True:
        return _passed()
    return _failed(_reason_from_failed_reasons(payload, f"{evidence_name} evidence did not pass"))


def _dry_run_check(*, path: Path | None, min_dry_run_days: int) -> dict[str, bool | str | None]:
    if path is None:
        return _failed("missing dry_run evidence")
    payload = _read_json(path)
    if payload is None:
        return _failed("missing dry_run evidence")
    if payload.get("passed") is not True:
        return _failed(_reason_from_failed_reasons(payload, "dry_run evidence did not pass"))

    duration_days = payload.get("duration_days")
    if not isinstance(duration_days, int | float):
        return _failed("missing dry_run duration_days")
    if duration_days < min_dry_run_days:
        return _failed(f"dry_run duration below {min_dry_run_days} days")
    return _passed()


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


def _passed() -> dict[str, bool | str | None]:
    return {"passed": True, "reason": None}


def _failed(reason: str) -> dict[str, bool | str | None]:
    return {"passed": False, "reason": reason}


if __name__ == "__main__":
    raise SystemExit(main())

