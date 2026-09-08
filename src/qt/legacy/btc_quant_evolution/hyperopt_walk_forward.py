# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.candidate_acceptance import write_candidate_acceptance_report
from qt.legacy.btc_quant_evolution.candidate_package import write_candidate_package
from qt.legacy.btc_quant_evolution.hyperopt_pipeline import (
    HyperoptValidationRequest,
    default_hyperopt_spaces,
    run_hyperopt_validation,
    validate_feature_matrix_request,
)
from qt.legacy.btc_quant_evolution.walk_forward import (
    WalkForwardRequest,
    WalkForwardSplit,
    build_walk_forward_splits,
    evaluate_walk_forward,
)


@dataclass(frozen=True)
class HyperoptWalkForwardRequest:
    exchange: str
    pair: str
    timeframe: str
    strategy: str
    start: date
    end: date
    train_days: int
    test_days: int
    step_days: int
    epochs: int
    random_state: int
    min_trades: int
    spaces: tuple[str, ...]
    hyperopt_loss: str
    job_workers: int
    feature_matrix: str | None = None
    run_id_prefix: str | None = None
    skip_download: bool = False

    def __post_init__(self) -> None:
        _walk_forward_request(self)
        if self.epochs <= 0:
            raise ValueError("epochs must be positive")
        if self.random_state <= 0:
            raise ValueError("random_state must be positive")
        if self.min_trades <= 0:
            raise ValueError("min_trades must be positive")
        if not self.spaces:
            raise ValueError("at least one hyperopt space is required")
        if self.job_workers == 0:
            raise ValueError("job_workers cannot be zero")
        if self.run_id_prefix is not None and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.run_id_prefix)
            or self.run_id_prefix in (".", "..")
        ):
            raise ValueError("run_id_prefix must be a path-safe identifier")
        validate_feature_matrix_request(strategy=self.strategy, feature_matrix=self.feature_matrix)


def write_hyperopt_walk_forward_plan(
    *,
    output_dir: Path,
    request: HyperoptWalkForwardRequest,
    max_splits: int | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_hyperopt_walk_forward_plan(request=request, max_splits=max_splits)
    plan_path = output_dir / "hyperopt_walk_forward_plan.json"
    plan_path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return plan_path


def build_hyperopt_walk_forward_plan(
    *,
    request: HyperoptWalkForwardRequest,
    max_splits: int | None = None,
) -> dict[str, Any]:
    return {
        "max_splits": max_splits,
        "request": _request_dict(request),
        "splits": [
            _split_plan_dict(request, split)
            for split in _selected_splits(request, max_splits=max_splits)
        ],
    }


def validate_hyperopt_walk_forward_evidence(
    *,
    request: HyperoptWalkForwardRequest,
    max_splits: int | None,
    plan: object,
    result: object,
) -> None:
    expected_plan = build_hyperopt_walk_forward_plan(
        request=request,
        max_splits=max_splits,
    )
    if plan != expected_plan:
        raise ValueError("rolling hyperopt plan does not match production config")
    if not isinstance(result, dict) or not isinstance(result.get("splits"), list):
        raise ValueError("rolling hyperopt result splits are malformed")
    result_splits = result["splits"]
    selected_splits = _selected_splits(request, max_splits=max_splits)
    if len(result_splits) != len(selected_splits):
        raise ValueError("rolling hyperopt result split count does not match production config")
    for expected_split, result_split in zip(selected_splits, result_splits, strict=True):
        if not isinstance(result_split, dict):
            raise ValueError(f"rolling hyperopt split {expected_split.index} is malformed")
        expected_identity = expected_split.as_dict()
        actual_identity = {
            key: result_split.get(key)
            for key in ("index", "test_timerange", "train_timerange")
        }
        if actual_identity != expected_identity:
            raise ValueError(
                f"rolling hyperopt split {expected_split.index} geometry does not match production config"
            )
        optimization_result = result_split.get("optimization_result")
        actual_request = (
            optimization_result.get("request")
            if isinstance(optimization_result, dict)
            else None
        )
        expected_request = asdict(
            _optimization_request(
                request,
                expected_split,
                exchange_check=expected_split.index == 1,
            )
        )
        expected_request["spaces"] = list(expected_request["spaces"])
        if actual_request != expected_request:
            raise ValueError(
                f"rolling hyperopt split {expected_split.index} optimization request "
                "does not match production config"
            )


def run_hyperopt_walk_forward_validation(
    *,
    output_dir: Path,
    request: HyperoptWalkForwardRequest,
    root_dir: Path,
    max_splits: int | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_hyperopt_walk_forward_plan(
        output_dir=output_dir,
        request=request,
        max_splits=max_splits,
    )

    split_results = []
    for split in _selected_splits(request, max_splits=max_splits):
        optimization_request = _optimization_request(request, split, exchange_check=split.index == 1)
        result_path = run_hyperopt_validation(request=optimization_request, root_dir=root_dir)
        optimization_result = json.loads(result_path.read_text())
        oos_run_dir = Path(str(optimization_result["out_of_sample_run_dir"]))
        summary = json.loads((oos_run_dir / "summary.json").read_text())
        robustness = json.loads((oos_run_dir / "robustness.json").read_text())
        regime = json.loads((oos_run_dir / "regime.json").read_text())
        split_results.append(
            {
                **split.as_dict(),
                "optimization_result": optimization_result,
                "regime": regime,
                "result_path": str(result_path),
                "robustness": robustness,
                "summary": summary,
            }
        )

    payload = {
        "evaluation": evaluate_walk_forward(split_results),
        "splits": split_results,
    }
    result_path = output_dir / "hyperopt_walk_forward_result.json"
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    candidate_report_path = write_candidate_acceptance_report(
        result_path=result_path,
        root_dir=root_dir,
    )
    payload["candidate_acceptance"] = json.loads(candidate_report_path.read_text())
    payload["candidate_package_path"] = str(result_path.with_name("candidate_package.json"))
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    write_candidate_package(result_path=result_path, root_dir=root_dir)
    return result_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run rolling hyperopt train/test walk-forward validation.")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--pair", default="BTC/USDT")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--strategy", default="BtcDonchianAtr")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--train-days", type=int, default=365)
    parser.add_argument("--test-days", type=int, default=90)
    parser.add_argument("--step-days", type=int, default=90)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--min-trades", type=int, default=50)
    parser.add_argument("--spaces", nargs="+")
    parser.add_argument("--hyperopt-loss", default="MultiMetricHyperOptLoss")
    parser.add_argument("--job-workers", type=int, default=1)
    parser.add_argument("--feature-matrix")
    parser.add_argument("--run-id-prefix")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--max-splits", type=int)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/hyperopt-walk-forward"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    request = HyperoptWalkForwardRequest(
        exchange=args.exchange,
        pair=args.pair,
        timeframe=args.timeframe,
        strategy=args.strategy,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
        epochs=args.epochs,
        random_state=args.random_state,
        min_trades=args.min_trades,
        spaces=tuple(args.spaces) if args.spaces is not None else default_hyperopt_spaces(args.strategy),
        hyperopt_loss=args.hyperopt_loss,
        job_workers=args.job_workers,
        feature_matrix=args.feature_matrix,
        run_id_prefix=args.run_id_prefix,
        skip_download=args.skip_download,
    )
    if args.plan_only:
        plan_path = write_hyperopt_walk_forward_plan(
            output_dir=args.output_dir,
            request=request,
            max_splits=args.max_splits,
        )
        print(plan_path)
        return 0

    result_path = run_hyperopt_walk_forward_validation(
        output_dir=args.output_dir,
        request=request,
        root_dir=args.root_dir.resolve(),
        max_splits=args.max_splits,
    )
    print(result_path)
    package = json.loads(result_path.with_name("candidate_package.json").read_text())
    return 0 if package.get("passed") is True else 1


def _optimization_request(
    request: HyperoptWalkForwardRequest,
    split: WalkForwardSplit,
    *,
    exchange_check: bool,
) -> HyperoptValidationRequest:
    return HyperoptValidationRequest(
        exchange=request.exchange,
        pair=request.pair,
        timeframe=request.timeframe,
        strategy=request.strategy,
        train_timerange=split.train_timerange,
        test_timerange=split.test_timerange,
        run_id=_split_run_id(split, request.run_id_prefix),
        epochs=request.epochs,
        random_state=request.random_state + split.index - 1,
        min_trades=request.min_trades,
        spaces=request.spaces,
        hyperopt_loss=request.hyperopt_loss,
        job_workers=request.job_workers,
        exchange_check=exchange_check and not request.skip_download,
        feature_matrix=request.feature_matrix,
        skip_download=request.skip_download,
    )


def _split_plan_dict(request: HyperoptWalkForwardRequest, split: WalkForwardSplit) -> dict[str, Any]:
    optimization_request = _optimization_request(request, split, exchange_check=split.index == 1)
    return {
        **split.as_dict(),
        "run_command": [
            ".venv/bin/python",
            "-m",
            "btc_quant.hyperopt_pipeline",
            "--exchange",
            optimization_request.exchange,
            "--pair",
            optimization_request.pair,
            "--timeframe",
            optimization_request.timeframe,
            "--strategy",
            optimization_request.strategy,
            "--train-timerange",
            optimization_request.train_timerange,
            "--test-timerange",
            optimization_request.test_timerange,
            "--run-id",
            optimization_request.run_id,
            "--epochs",
            str(optimization_request.epochs),
            "--random-state",
            str(optimization_request.random_state),
            "--min-trades",
            str(optimization_request.min_trades),
            "--spaces",
            *optimization_request.spaces,
            "--hyperopt-loss",
            optimization_request.hyperopt_loss,
            "--job-workers",
            str(optimization_request.job_workers),
            *(
                ["--feature-matrix", optimization_request.feature_matrix]
                if optimization_request.feature_matrix is not None
                else []
            ),
            *(["--skip-download"] if optimization_request.skip_download else []),
        ],
    }


def _split_run_id(split: WalkForwardSplit, run_id_prefix: str | None = None) -> str:
    base_run_id = f"hwf-{split.index:03d}-{split.test_timerange}"
    return f"{run_id_prefix}-{base_run_id}" if run_id_prefix is not None else base_run_id


def _splits(request: HyperoptWalkForwardRequest) -> list[WalkForwardSplit]:
    return build_walk_forward_splits(_walk_forward_request(request))


def _selected_splits(
    request: HyperoptWalkForwardRequest,
    *,
    max_splits: int | None,
) -> list[WalkForwardSplit]:
    splits = _splits(request)
    if max_splits is None:
        return splits
    if max_splits <= 0:
        raise ValueError("max_splits must be positive")
    return splits[:max_splits]


def _walk_forward_request(request: HyperoptWalkForwardRequest) -> WalkForwardRequest:
    return WalkForwardRequest(
        exchange=request.exchange,
        pair=request.pair,
        timeframe=request.timeframe,
        strategy=request.strategy,
        start=request.start,
        end=request.end,
        train_days=request.train_days,
        test_days=request.test_days,
        step_days=request.step_days,
    )


def _request_dict(request: HyperoptWalkForwardRequest) -> dict[str, Any]:
    payload = asdict(request)
    payload["end"] = request.end.isoformat()
    payload["spaces"] = list(request.spaces)
    payload["start"] = request.start.isoformat()
    return payload


if __name__ == "__main__":
    raise SystemExit(main())

