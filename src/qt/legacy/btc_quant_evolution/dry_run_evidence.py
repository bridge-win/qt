# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.runtime_evidence import (
    load_dry_run_manifest_anchor,
    validate_dry_run_launch_receipt,
)

DEFAULT_MIN_DURATION_DAYS = 21
LOG_TIMESTAMP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:,\d{3}|Z)?")
STARTUP_MARKERS = ("bot started", "changing state to: running")
STARTUP_GRACE = timedelta(minutes=5)
FUTURE_TIMESTAMP_GRACE = timedelta(minutes=5)


def evaluate_dry_run_evidence(
    *,
    config: dict[str, Any],
    log_text: str,
    min_duration_days: int = DEFAULT_MIN_DURATION_DAYS,
    launch_started_at: datetime | None = None,
    launch_completed_at: datetime | None = None,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    entries = _parse_log_entries(log_text)
    timestamps = [timestamp for timestamp, _ in entries]
    missing_launch_startup = False
    if launch_started_at is not None or launch_completed_at is not None:
        if launch_started_at is None or launch_completed_at is None:
            raise ValueError("dry-run launch interval is incomplete")
        startup_at = _find_launch_startup(
            entries,
            launch_started_at=launch_started_at,
            launch_completed_at=launch_completed_at,
        )
        missing_launch_startup = startup_at is None
        timestamps = (
            [timestamp for timestamp, _ in entries if timestamp >= startup_at]
            if startup_at is not None
            else []
        )
    future_timestamps: list[datetime] = []
    if evaluated_at is not None:
        if evaluated_at.tzinfo is None:
            raise ValueError("dry-run evidence evaluation time must be timezone-aware")
        latest_allowed = evaluated_at.astimezone(timezone.utc) + FUTURE_TIMESTAMP_GRACE
        future_timestamps = [timestamp for timestamp in timestamps if timestamp > latest_allowed]
        timestamps = [timestamp for timestamp in timestamps if timestamp <= latest_allowed]
    first_log_at = min(timestamps) if timestamps else None
    last_log_at = max(timestamps) if timestamps else None
    duration_days = _duration_days(first_log_at, last_log_at)
    failed_reasons = []

    if config.get("dry_run") is not True:
        failed_reasons.append("config dry_run is not true")
    if missing_launch_startup:
        failed_reasons.append("missing dry_run startup timestamp near launch")
    elif not timestamps:
        failed_reasons.append("missing parseable freqtrade log timestamps")
    if future_timestamps:
        failed_reasons.append("dry_run log timestamp is in the future")
    if duration_days < min_duration_days:
        failed_reasons.append(f"dry_run duration below {min_duration_days} days")

    return {
        "duration_days": duration_days,
        "failed_reasons": failed_reasons,
        "first_log_at": _format_timestamp(first_log_at) if first_log_at is not None else None,
        "last_log_at": _format_timestamp(last_log_at) if last_log_at is not None else None,
        "min_duration_days": min_duration_days,
        "passed": not failed_reasons,
    }


def write_dry_run_evidence(
    *,
    output_dir: Path,
    root_dir: Path,
    config_path: Path,
    log_path: Path,
    min_duration_days: int = DEFAULT_MIN_DURATION_DAYS,
    candidate_package_path: Path | None = None,
    launch_receipt_path: Path | None = None,
) -> Path:
    config = json.loads(config_path.read_text())
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    receipt: dict[str, Any] | None = None
    receipt_artifact: dict[str, Any] | None = None
    manifest_anchor: dict[str, Any] | None = None
    generated_at: datetime | None = None
    if candidate_package_path is not None and _package_schema(candidate_package_path) == 3:
        if launch_receipt_path is None:
            raise ValueError("schema-3 candidate launch receipt is required")
        receipt, receipt_artifact = validate_dry_run_launch_receipt(
            root_dir=root_dir,
            receipt_path=launch_receipt_path,
            candidate_package_path=candidate_package_path,
            config_path=config_path,
            log_path=log_path,
        )
        manifest_anchor = load_dry_run_manifest_anchor(
            root_dir=root_dir,
            candidate_package_path=candidate_package_path,
            receipt_path=launch_receipt_path,
            receipt=receipt,
        )
        generated_at = datetime.now(timezone.utc)
    result = evaluate_dry_run_evidence(
        config=config,
        log_text=log_path.read_text(),
        min_duration_days=min_duration_days,
        launch_started_at=_receipt_timestamp(receipt, "launch_started_at"),
        launch_completed_at=_receipt_timestamp(receipt, "launch_completed_at"),
        evaluated_at=generated_at,
    )
    report = {
        **result,
        "config_path": _relative_path(path=config_path, root_dir=root_dir),
        "log_path": _relative_path(path=log_path, root_dir=root_dir),
    }
    if receipt is not None and receipt_artifact is not None:
        report["runtime_selection"] = receipt["runtime_selection"]
        report["runtime_strategy_sources"] = receipt["runtime_strategy_sources"]
        report["launch_receipt_artifact"] = receipt_artifact
        report["production_manifest_anchor"] = manifest_anchor
        report["generated_at"] = _format_timestamp(generated_at)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "dry_run.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report_path


def validate_schema_three_dry_run_evidence(
    *,
    root_dir: Path,
    candidate_package_path: Path,
    payload: dict[str, Any],
) -> str | None:
    receipt_artifact = payload.get("launch_receipt_artifact")
    config_path_text = payload.get("config_path")
    log_path_text = payload.get("log_path")
    if (
        not isinstance(receipt_artifact, dict)
        or not isinstance(receipt_artifact.get("path"), str)
        or not isinstance(config_path_text, str)
        or not isinstance(log_path_text, str)
    ):
        return "missing schema-3 dry-run launch receipt binding"
    receipt_path = root_dir / receipt_artifact["path"]
    validated_at = datetime.now(timezone.utc)
    try:
        receipt, current_artifact = validate_dry_run_launch_receipt(
            root_dir=root_dir,
            receipt_path=receipt_path,
            candidate_package_path=candidate_package_path,
            config_path=root_dir / config_path_text,
            log_path=root_dir / log_path_text,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return str(exc)
    if current_artifact != receipt_artifact:
        return "dry-run launch receipt artifact does not match current file"
    if payload.get("runtime_selection") != receipt.get("runtime_selection"):
        return "runtime selection does not match candidate package"
    if payload.get("runtime_strategy_sources") != receipt.get("runtime_strategy_sources"):
        return "dry-run strategy provenance does not match candidate package"
    try:
        current_anchor = load_dry_run_manifest_anchor(
            root_dir=root_dir,
            candidate_package_path=candidate_package_path,
            receipt_path=receipt_path,
            receipt=receipt,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return str(exc)
    if payload.get("production_manifest_anchor") != current_anchor:
        return "dry-run production manifest anchor does not match current manifest"
    generated_at = _receipt_timestamp(payload, "generated_at")
    if generated_at is None or generated_at > validated_at + FUTURE_TIMESTAMP_GRACE:
        return "dry-run evidence generation timestamp is in the future"
    try:
        config = json.loads((root_dir / config_path_text).read_text())
        if not isinstance(config, dict):
            return "dry-run config must be a JSON object"
        current = evaluate_dry_run_evidence(
            config=config,
            log_text=(root_dir / log_path_text).read_text(),
            min_duration_days=_required_int(payload, "min_duration_days"),
            launch_started_at=_receipt_timestamp(receipt, "launch_started_at"),
            launch_completed_at=_receipt_timestamp(receipt, "launch_completed_at"),
            evaluated_at=validated_at,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return str(exc)
    if current.get("passed") is not True:
        return "current dry-run log no longer proves the recorded launch duration"
    recorded_duration = payload.get("duration_days")
    current_duration = current.get("duration_days")
    recorded_last = payload.get("last_log_at")
    current_last = current.get("last_log_at")
    if (
        not isinstance(recorded_duration, int | float)
        or isinstance(recorded_duration, bool)
        or not isinstance(current_duration, int | float)
        or isinstance(current_duration, bool)
        or not isinstance(recorded_last, str)
        or not isinstance(current_last, str)
        or payload.get("first_log_at") != current.get("first_log_at")
        or recorded_last > current_last
        or recorded_duration > current_duration
    ):
        return "dry-run evidence interval does not match launch-attributed log timestamps"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Write dry-run duration evidence from Freqtrade config and log.")
    parser.add_argument("--config", type=Path, default=Path("user_data/config.json"))
    parser.add_argument("--log", type=Path, default=Path("user_data/logs/freqtrade.log"))
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/live-evidence"))
    parser.add_argument("--min-duration-days", type=int, default=DEFAULT_MIN_DURATION_DAYS)
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    parser.add_argument("--candidate-package", type=Path, default=None)
    parser.add_argument("--launch-receipt", type=Path, default=None)
    args = parser.parse_args()

    report_path = write_dry_run_evidence(
        output_dir=args.output_dir,
        root_dir=args.root_dir.resolve(),
        config_path=args.config,
        log_path=args.log,
        min_duration_days=args.min_duration_days,
        candidate_package_path=args.candidate_package,
        launch_receipt_path=args.launch_receipt,
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] is True else 1


def _parse_log_entries(log_text: str) -> list[tuple[datetime, str]]:
    entries = []
    for line in log_text.splitlines():
        match = LOG_TIMESTAMP_PATTERN.match(line)
        if match is None:
            continue
        timestamp_text = match.group(1).replace("T", " ")
        timestamp = datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        entries.append((timestamp, line))
    return entries


def _find_launch_startup(
    entries: list[tuple[datetime, str]],
    *,
    launch_started_at: datetime,
    launch_completed_at: datetime,
) -> datetime | None:
    latest_startup = launch_completed_at + STARTUP_GRACE
    for timestamp, line in entries:
        normalized = line.casefold()
        if (
            launch_started_at <= timestamp <= latest_startup
            and any(marker in normalized for marker in STARTUP_MARKERS)
        ):
            return timestamp
    return None


def _package_schema(candidate_package_path: Path) -> object:
    package = json.loads(candidate_package_path.read_text())
    if not isinstance(package, dict):
        raise ValueError("candidate package must be a JSON object")
    return package.get("schema_version")


def _receipt_timestamp(receipt: dict[str, Any] | None, key: str) -> datetime | None:
    if receipt is None:
        return None
    value = receipt.get(key)
    if not isinstance(value, str):
        raise ValueError("dry-run launch receipt timestamp is malformed")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("dry-run launch receipt timestamp is malformed")
    return parsed.astimezone(timezone.utc)


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"dry-run evidence {key} is malformed")
    return value


def _duration_days(started_at: datetime | None, completed_at: datetime | None) -> float:
    if started_at is None or completed_at is None:
        return 0.0
    return round((completed_at - started_at).total_seconds() / 86_400, 2)


def _relative_path(*, path: Path, root_dir: Path) -> str:
    try:
        return path.resolve().relative_to(root_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

