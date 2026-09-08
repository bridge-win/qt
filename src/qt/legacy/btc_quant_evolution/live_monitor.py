# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from qt.legacy.btc_quant_evolution.live_market import (
    LiveMarketPolicy,
    evaluate_live_market_snapshot,
    fetch_live_market_snapshot,
)


@dataclass(frozen=True)
class LiveMonitorPolicy:
    min_samples: int
    min_pass_ratio: float
    max_consecutive_failures: int

    def __post_init__(self) -> None:
        if self.min_samples <= 0:
            raise ValueError("min_samples must be positive")
        if self.min_pass_ratio < 0 or self.min_pass_ratio > 1:
            raise ValueError("min_pass_ratio must be between 0 and 1")
        if self.max_consecutive_failures < 0:
            raise ValueError("max_consecutive_failures cannot be negative")


def evaluate_live_market_monitor(
    *,
    samples: list[dict[str, Any]],
    policy: LiveMonitorPolicy,
) -> dict[str, Any]:
    sample_count = len(samples)
    passed_samples = sum(1 for sample in samples if _sample_passed(sample))
    failed_samples = sample_count - passed_samples
    pass_ratio = round(passed_samples / sample_count, 2) if sample_count else 0.0
    max_consecutive_failures = _max_consecutive_failures(samples)
    failed_reasons = []

    if sample_count < policy.min_samples:
        failed_reasons.append(f"sample_count below {policy.min_samples}")
    if pass_ratio < policy.min_pass_ratio:
        failed_reasons.append(f"pass_ratio below {policy.min_pass_ratio:g}")
    if max_consecutive_failures > policy.max_consecutive_failures:
        failed_reasons.append(f"max_consecutive_failures above {policy.max_consecutive_failures}")

    return {
        "failed_reasons": failed_reasons,
        "failed_samples": failed_samples,
        "max_consecutive_failures": max_consecutive_failures,
        "max_ticker_staleness_seconds": _max_metric(samples, "ticker_staleness_seconds"),
        "pass_ratio": pass_ratio,
        "passed": not failed_reasons,
        "passed_samples": passed_samples,
        "sample_count": sample_count,
        "worst_spread_bps": _max_metric(samples, "spread_bps"),
    }


def write_live_market_monitor_report(
    *,
    output_dir: Path,
    exchange: str,
    pair: str,
    started_at: datetime,
    completed_at: datetime,
    snapshot_policy: LiveMarketPolicy,
    monitor_policy: LiveMonitorPolicy,
    samples: list[dict[str, Any]],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    samples_path = output_dir / "live_market_samples.jsonl"
    samples_path.write_text(
        "".join(json.dumps(sample, sort_keys=True, allow_nan=False) + "\n" for sample in samples)
    )
    report = {
        "completed_at": _format_timestamp(_normalize_datetime(completed_at)),
        "exchange": exchange,
        "monitor_policy": asdict(monitor_policy),
        "pair": pair,
        "result": evaluate_live_market_monitor(samples=samples, policy=monitor_policy),
        "samples_path": samples_path.name,
        "snapshot_policy": asdict(snapshot_policy),
        "started_at": _format_timestamp(_normalize_datetime(started_at)),
    }
    report_path = output_dir / "live_market_monitor.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report_path


def run_live_market_monitor(
    *,
    output_dir: Path,
    exchange: str,
    pair: str,
    snapshot_policy: LiveMarketPolicy,
    monitor_policy: LiveMonitorPolicy,
    root_dir: Path,
    interval_seconds: float,
    fetch_snapshot: Callable[..., dict[str, Any]] = fetch_live_market_snapshot,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    if interval_seconds < 0:
        raise ValueError("interval_seconds cannot be negative")

    started_at = now()
    samples = []
    for index in range(monitor_policy.min_samples):
        if index:
            sleep(interval_seconds)
        snapshot = fetch_snapshot(
            exchange=exchange,
            pair=pair,
            orderbook_limit=snapshot_policy.min_orderbook_levels,
            root_dir=root_dir,
        )
        captured_at = now()
        samples.append(
            {
                "captured_at": _format_timestamp(captured_at),
                "result": evaluate_live_market_snapshot(
                    snapshot=snapshot,
                    captured_at=captured_at,
                    policy=snapshot_policy,
                ),
                "snapshot": snapshot,
            }
        )

    return write_live_market_monitor_report(
        output_dir=output_dir,
        exchange=exchange,
        pair=pair,
        started_at=started_at,
        completed_at=now(),
        snapshot_policy=snapshot_policy,
        monitor_policy=monitor_policy,
        samples=samples,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Repeatedly capture and validate live public market snapshots.")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--pair", default="BTC/USDT")
    parser.add_argument("--samples", type=int, default=6)
    parser.add_argument("--interval-seconds", type=float, default=10)
    parser.add_argument("--min-pass-ratio", type=float, default=1)
    parser.add_argument("--max-consecutive-failures", type=int, default=0)
    parser.add_argument("--max-ticker-staleness-seconds", type=float, default=30)
    parser.add_argument("--max-orderbook-staleness-seconds", type=float, default=30)
    parser.add_argument("--max-spread-bps", type=float, default=10)
    parser.add_argument("--min-orderbook-levels", type=int, default=5)
    parser.add_argument("--max-clock-skew-seconds", type=float, default=5)
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/live-market-monitor"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    report_path = run_live_market_monitor(
        output_dir=args.output_dir,
        exchange=args.exchange,
        pair=args.pair,
        snapshot_policy=LiveMarketPolicy(
            max_ticker_staleness_seconds=args.max_ticker_staleness_seconds,
            max_orderbook_staleness_seconds=args.max_orderbook_staleness_seconds,
            max_spread_bps=args.max_spread_bps,
            min_orderbook_levels=args.min_orderbook_levels,
            max_clock_skew_seconds=args.max_clock_skew_seconds,
        ),
        monitor_policy=LiveMonitorPolicy(
            min_samples=args.samples,
            min_pass_ratio=args.min_pass_ratio,
            max_consecutive_failures=args.max_consecutive_failures,
        ),
        root_dir=args.root_dir.resolve(),
        interval_seconds=args.interval_seconds,
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"]["passed"] else 1


def _sample_passed(sample: dict[str, Any]) -> bool:
    result = sample.get("result")
    return isinstance(result, dict) and result.get("passed") is True


def _max_consecutive_failures(samples: list[dict[str, Any]]) -> int:
    current = 0
    maximum = 0
    for sample in samples:
        if _sample_passed(sample):
            current = 0
            continue
        current += 1
        maximum = max(maximum, current)
    return maximum


def _max_metric(samples: list[dict[str, Any]], metric_name: str) -> float | None:
    values = []
    for sample in samples:
        result = sample.get("result")
        if not isinstance(result, dict):
            continue
        value = result.get(metric_name)
        if isinstance(value, int | float):
            values.append(float(value))
    return max(values) if values else None


def _normalize_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

