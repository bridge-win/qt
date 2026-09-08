# Migrated with Python 3.10 UTC compatibility from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MIN_CANDLES = 20586


@dataclass(frozen=True)
class DataQualityPolicy:
    min_candles: int
    max_staleness_hours: float | None

    def __post_init__(self) -> None:
        if self.min_candles <= 0:
            raise ValueError("min_candles must be positive")
        if self.max_staleness_hours is not None and self.max_staleness_hours < 0:
            raise ValueError("max_staleness_hours cannot be negative")


def build_list_data_command(*, exchange: str, pair: str) -> list[str]:
    return [
        "docker",
        "compose",
        "run",
        "--rm",
        "freqtrade",
        "list-data",
        "--exchange",
        exchange,
        "--pairs",
        pair,
        "--trading-mode",
        "spot",
        "--data-format-ohlcv",
        "feather",
        "--show-timerange",
    ]


def run_data_quality_check(
    *,
    output_dir: Path,
    exchange: str,
    pair: str,
    timeframe: str,
    policy: DataQualityPolicy,
    as_of: datetime,
    root_dir: Path,
) -> Path:
    command = build_list_data_command(exchange=exchange, pair=pair)
    completed = subprocess.run(
        command,
        cwd=root_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
        text=True,
    )
    result = evaluate_data_quality(
        coverages=parse_list_data_output(completed.stdout),
        pair=pair,
        timeframe=timeframe,
        policy=policy,
        as_of=as_of,
    )
    report = {
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "command": command,
        "exchange": exchange,
        "pair": pair,
        "policy": asdict(policy),
        "result": result,
        "timeframe": timeframe,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "data_quality.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report_path


def parse_list_data_output(output: str) -> list[dict[str, Any]]:
    coverages: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("│") or "Candles" in stripped:
            continue

        parts = [part.strip() for part in stripped.strip("│").split("│")]
        if len(parts) != 6:
            continue

        pair, timeframe, trading_mode, start_text, end_text, candles_text = parts
        if "/" not in pair:
            continue

        coverages.append(
            {
                "candles": int(candles_text),
                "end": _parse_freqtrade_datetime(end_text).isoformat().replace("+00:00", "Z"),
                "pair": pair,
                "start": _parse_freqtrade_datetime(start_text).isoformat().replace("+00:00", "Z"),
                "timeframe": timeframe,
                "trading_mode": trading_mode,
            }
        )

    return coverages


def evaluate_data_quality(
    *,
    coverages: list[dict[str, Any]],
    pair: str,
    timeframe: str,
    policy: DataQualityPolicy,
    as_of: datetime,
) -> dict[str, Any]:
    coverage = next(
        (
            item
            for item in coverages
            if item.get("pair") == pair and item.get("timeframe") == timeframe
        ),
        None,
    )
    if coverage is None:
        return {
            "coverage": None,
            "failed_reasons": [f"missing coverage for {pair} {timeframe}"],
            "passed": False,
        }

    failed_reasons = []
    if coverage["candles"] < policy.min_candles:
        failed_reasons.append(f"candles below {policy.min_candles}")

    if policy.max_staleness_hours is not None:
        normalized_as_of = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(coverage["end"].replace("Z", "+00:00"))
        stale_hours = (normalized_as_of - end).total_seconds() / 3600
        if stale_hours > policy.max_staleness_hours:
            failed_reasons.append(f"latest candle is stale by {stale_hours:.2f} hours")

    return {
        "coverage": coverage,
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
    }


def _parse_freqtrade_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check downloaded Freqtrade data coverage.")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--pair", default="BTC/USDT")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--min-candles", type=int, default=DEFAULT_MIN_CANDLES)
    parser.add_argument("--max-staleness-hours", type=float, default=48)
    parser.add_argument("--skip-staleness-check", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/data-quality"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    report_path = run_data_quality_check(
        output_dir=args.output_dir,
        exchange=args.exchange,
        pair=args.pair,
        timeframe=args.timeframe,
        policy=DataQualityPolicy(
            min_candles=args.min_candles,
            max_staleness_hours=None if args.skip_staleness_check else args.max_staleness_hours,
        ),
        as_of=datetime.now(timezone.utc),
        root_dir=args.root_dir.resolve(),
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

