# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

DEFAULT_OUTPUT_TAIL_CHARS = 4000


def write_command_evidence(
    *,
    output_dir: Path,
    name: str,
    command: list[str],
    root_dir: Path,
    output_tail_chars: int = DEFAULT_OUTPUT_TAIL_CHARS,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    if not name:
        raise ValueError("name is required")
    if not command:
        raise ValueError("command is required")
    if output_tail_chars <= 0:
        raise ValueError("output_tail_chars must be positive")

    started_at = now()
    completed = run(
        args=command,
        cwd=root_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    completed_at = now()
    exit_code = int(completed.returncode)
    failed_reasons = [] if exit_code == 0 else [f"{name} command exited with {exit_code}"]
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "command": command,
        "completed_at": _format_timestamp(completed_at),
        "cwd": ".",
        "exit_code": exit_code,
        "failed_reasons": failed_reasons,
        "name": name,
        "output_tail": str(completed.stdout)[-output_tail_chars:],
        "passed": exit_code == 0,
        "started_at": _format_timestamp(started_at),
    }
    report_path = output_dir / f"{name}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a command and write machine-readable evidence JSON.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/live-evidence"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    parser.add_argument("--output-tail-chars", type=int, default=DEFAULT_OUTPUT_TAIL_CHARS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    report_path = write_command_evidence(
        output_dir=args.output_dir,
        name=args.name,
        command=command,
        root_dir=args.root_dir.resolve(),
        output_tail_chars=args.output_tail_chars,
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] is True else 1


def _format_timestamp(value: datetime) -> str:
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

