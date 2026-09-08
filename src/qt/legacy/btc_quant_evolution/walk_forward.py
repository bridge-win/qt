# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.research_pipeline import BacktestRequest, run_research_plan


@dataclass(frozen=True)
class WalkForwardRequest:
    exchange: str
    pair: str
    timeframe: str
    strategy: str
    start: date
    end: date
    train_days: int
    test_days: int
    step_days: int

    def __post_init__(self) -> None:
        if not self.exchange:
            raise ValueError("exchange is required")
        if not self.pair:
            raise ValueError("pair is required")
        if not self.timeframe:
            raise ValueError("timeframe is required")
        if not self.strategy:
            raise ValueError("strategy is required")
        if self.train_days <= 0:
            raise ValueError("train_days must be positive")
        if self.test_days <= 0:
            raise ValueError("test_days must be positive")
        if self.step_days <= 0:
            raise ValueError("step_days must be positive")
        if self.start >= self.end:
            raise ValueError("start must be before end")


@dataclass(frozen=True)
class WalkForwardSplit:
    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date

    @property
    def train_timerange(self) -> str:
        return _timerange(self.train_start, self.train_end)

    @property
    def test_timerange(self) -> str:
        return _timerange(self.test_start, self.test_end)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "test_timerange": self.test_timerange,
            "train_timerange": self.train_timerange,
        }


def build_walk_forward_splits(request: WalkForwardRequest) -> list[WalkForwardSplit]:
    splits: list[WalkForwardSplit] = []
    train_start = request.start
    index = 1

    while True:
        train_end = train_start + timedelta(days=request.train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=request.test_days)
        if test_end > request.end:
            break

        splits.append(
            WalkForwardSplit(
                index=index,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        train_start += timedelta(days=request.step_days)
        index += 1

    return splits


def evaluate_walk_forward(split_results: list[dict[str, Any]]) -> dict[str, Any]:
    failed_reasons = []
    passed_count = 0
    summaries = []
    for index, split_result in enumerate(split_results, start=1):
        summary = _summary_from_split_result(split_result)
        summaries.append(summary)
        gate = summary.get("production_gate")
        summary_passed = isinstance(gate, dict) and gate.get("passed") is True
        if _contains_non_finite(summary):
            summary_passed = False
            failed_reasons.append(f"split {index} failed: non-finite production metrics")
        elif not summary_passed:
            reasons = gate.get("failed_reasons") if isinstance(gate, dict) else ["missing production gate"]
            joined = ", ".join(str(reason) for reason in reasons)
            failed_reasons.append(f"split {index} failed: {joined}")

        robustness = _robustness_from_split_result(split_result)
        robustness_gate = robustness.get("robustness_gate") if isinstance(robustness, dict) else None
        robustness_passed = isinstance(robustness_gate, dict) and robustness_gate.get("passed") is True
        if _contains_non_finite(robustness):
            robustness_passed = False
            failed_reasons.append(f"split {index} robustness failed: non-finite metrics")
        elif not robustness_passed:
            reasons = (
                robustness_gate.get("failed_reasons")
                if isinstance(robustness_gate, dict)
                else ["missing robustness gate"]
            )
            joined = ", ".join(str(reason) for reason in reasons)
            failed_reasons.append(f"split {index} robustness failed: {joined}")

        regime = _regime_from_split_result(split_result)
        regime_gate = regime.get("regime_gate") if isinstance(regime, dict) else None
        regime_passed = isinstance(regime_gate, dict) and regime_gate.get("passed") is True
        if _contains_non_finite(regime):
            regime_passed = False
            failed_reasons.append(f"split {index} regime failed: non-finite metrics")
        elif not regime_passed:
            reasons = (
                regime_gate.get("failed_reasons")
                if isinstance(regime_gate, dict)
                else ["missing regime gate"]
            )
            joined = ", ".join(str(reason) for reason in reasons)
            failed_reasons.append(f"split {index} regime failed: {joined}")

        if summary_passed and robustness_passed and regime_passed:
            passed_count += 1

    return {
        "aggregate": _aggregate_split_results(summaries, passed_count=passed_count),
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
        "split_count": len(split_results),
    }


def write_walk_forward_plan(*, output_dir: Path, request: WalkForwardRequest) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "request": _request_dict(request),
        "splits": [_split_plan_dict(request, split) for split in build_walk_forward_splits(request)],
    }
    plan_path = output_dir / "walk_forward_plan.json"
    plan_path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return plan_path


def run_walk_forward_validation(
    *,
    output_dir: Path,
    request: WalkForwardRequest,
    root_dir: Path,
    max_splits: int | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_walk_forward_plan(output_dir=output_dir, request=request)

    split_results = []
    for split in _selected_splits(request, max_splits=max_splits):
        backtest_request = BacktestRequest(
            exchange=request.exchange,
            pair=request.pair,
            timeframe=request.timeframe,
            days=None,
            timerange=split.test_timerange,
            strategy=request.strategy,
            run_id=f"wf-{split.index:03d}-{split.test_timerange}",
            include_bias_checks=True,
            exchange_check=split.index == 1,
        )
        run_dir = run_research_plan(request=backtest_request, root_dir=root_dir)
        summary = json.loads((run_dir / "summary.json").read_text())
        robustness = json.loads((run_dir / "robustness.json").read_text())
        regime = json.loads((run_dir / "regime.json").read_text())
        split_results.append(
            {
                **split.as_dict(),
                "regime": regime,
                "robustness": robustness,
                "run_dir": str(run_dir),
                "summary": summary,
            }
        )

    payload = {
        "evaluation": evaluate_walk_forward(split_results),
        "splits": split_results,
    }
    result_path = output_dir / "walk_forward_result.json"
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return result_path


def _timerange(start: date, end: date) -> str:
    return f"{start:%Y%m%d}-{end:%Y%m%d}"


def _split_plan_dict(request: WalkForwardRequest, split: WalkForwardSplit) -> dict[str, Any]:
    return {
        **split.as_dict(),
        "run_command": [
            ".venv/bin/python",
            "-m",
            "btc_quant.research_pipeline",
            "--exchange",
            request.exchange,
            "--pair",
            request.pair,
            "--timeframe",
            request.timeframe,
            "--timerange",
            split.test_timerange,
            "--strategy",
            request.strategy,
            "--run-id",
            f"wf-{split.index:03d}-{split.test_timerange}",
        ],
    }


def _request_dict(request: WalkForwardRequest) -> dict[str, Any]:
    return {
        "end": request.end.isoformat(),
        "exchange": request.exchange,
        "pair": request.pair,
        "start": request.start.isoformat(),
        "step_days": request.step_days,
        "strategy": request.strategy,
        "test_days": request.test_days,
        "timeframe": request.timeframe,
        "train_days": request.train_days,
    }


def _selected_splits(
    request: WalkForwardRequest,
    *,
    max_splits: int | None,
) -> list[WalkForwardSplit]:
    splits = build_walk_forward_splits(request)
    if max_splits is None:
        return splits
    if max_splits <= 0:
        raise ValueError("max_splits must be positive")
    return splits[:max_splits]


def _aggregate_split_results(
    summaries: list[dict[str, Any]],
    *,
    passed_count: int,
) -> dict[str, float | None]:
    profit_values = _numeric_values(summaries, key="profit_total_pct")
    drawdown_values = _numeric_values(summaries, key="max_drawdown_pct")
    total_profit = sum(profit_values) if profit_values else None

    return {
        "average_profit_total_pct": total_profit / len(profit_values) if total_profit is not None else None,
        "pass_ratio": passed_count / len(summaries) if summaries else None,
        "total_profit_total_pct": total_profit,
        "worst_max_drawdown_pct": max(drawdown_values) if drawdown_values else None,
    }


def _numeric_values(summaries: list[dict[str, Any]], *, key: str) -> list[float]:
    values = []
    for summary in summaries:
        value = summary.get(key)
        if isinstance(value, (int, float)):
            numeric_value = float(value)
            if not math.isfinite(numeric_value):
                return []
            values.append(numeric_value)
    return values


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


def _summary_from_split_result(split_result: dict[str, Any]) -> dict[str, Any]:
    summary = split_result.get("summary")
    if isinstance(summary, dict):
        return summary
    return split_result


def _robustness_from_split_result(split_result: dict[str, Any]) -> dict[str, Any] | None:
    robustness = split_result.get("robustness")
    if isinstance(robustness, dict):
        return robustness
    return None


def _regime_from_split_result(split_result: dict[str, Any]) -> dict[str, Any] | None:
    regime = split_result.get("regime")
    if isinstance(regime, dict):
        return regime
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or execute a walk-forward validation plan.")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--pair", default="BTC/USDT")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--strategy", default="BtcDonchianAtr")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--train-days", type=int, default=365)
    parser.add_argument("--test-days", type=int, default=90)
    parser.add_argument("--step-days", type=int, default=90)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-splits", type=int)
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/walk-forward"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    request = WalkForwardRequest(
        exchange=args.exchange,
        pair=args.pair,
        timeframe=args.timeframe,
        strategy=args.strategy,
        start=_parse_date(args.start),
        end=_parse_date(args.end),
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
    )
    if args.execute:
        result_path = run_walk_forward_validation(
            output_dir=args.output_dir,
            request=request,
            root_dir=args.root_dir.resolve(),
            max_splits=args.max_splits,
        )
        print(result_path)
        result = json.loads(result_path.read_text())
        return 0 if result["evaluation"]["passed"] else 1

    plan_path = write_walk_forward_plan(output_dir=args.output_dir, request=request)
    print(plan_path)
    return 0


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


if __name__ == "__main__":
    raise SystemExit(main())

