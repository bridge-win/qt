# Migrated with Python 3.10 UTC compatibility from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MIN_DURATION_DAYS = 21
LOG_TIMESTAMP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:,\d{3}|Z)?")


def evaluate_dry_run_evidence(
    *,
    config: dict[str, Any],
    log_text: str,
    min_duration_days: int = DEFAULT_MIN_DURATION_DAYS,
) -> dict[str, Any]:
    timestamps = _parse_log_timestamps(log_text)
    first_log_at = min(timestamps) if timestamps else None
    last_log_at = max(timestamps) if timestamps else None
    duration_days = _duration_days(first_log_at, last_log_at)
    failed_reasons = []

    if config.get("dry_run") is not True:
        failed_reasons.append("config dry_run is not true")
    if not timestamps:
        failed_reasons.append("missing parseable freqtrade log timestamps")
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
) -> Path:
    config = json.loads(config_path.read_text())
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    result = evaluate_dry_run_evidence(
        config=config,
        log_text=log_path.read_text(),
        min_duration_days=min_duration_days,
    )
    report = {
        **result,
        "config_path": _relative_path(path=config_path, root_dir=root_dir),
        "log_path": _relative_path(path=log_path, root_dir=root_dir),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "dry_run.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Write dry-run duration evidence from Freqtrade config and log.")
    parser.add_argument("--config", type=Path, default=Path("user_data/config.json"))
    parser.add_argument("--log", type=Path, default=Path("user_data/logs/freqtrade.log"))
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/live-evidence"))
    parser.add_argument("--min-duration-days", type=int, default=DEFAULT_MIN_DURATION_DAYS)
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    report_path = write_dry_run_evidence(
        output_dir=args.output_dir,
        root_dir=args.root_dir.resolve(),
        config_path=args.config,
        log_path=args.log,
        min_duration_days=args.min_duration_days,
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] is True else 1


def _parse_log_timestamps(log_text: str) -> list[datetime]:
    timestamps = []
    for line in log_text.splitlines():
        match = LOG_TIMESTAMP_PATTERN.match(line)
        if match is None:
            continue
        timestamp_text = match.group(1).replace("T", " ")
        timestamps.append(datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
    return timestamps


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

