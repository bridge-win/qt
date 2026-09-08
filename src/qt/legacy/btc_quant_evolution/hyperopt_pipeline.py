# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.freqtrade_state_lock import freqtrade_state_lock
from qt.legacy.btc_quant_evolution.ohlcv_integrity import timeframe_to_seconds
from qt.legacy.btc_quant_evolution.research_pipeline import (
    CONFIG_PATH,
    FEATURE_MATRIX_STRATEGIES,
    STRATEGY_SOURCE_FILES,
    BacktestRequest,
    PipelineStep,
    run_research_plan,
)


@dataclass(frozen=True)
class HyperoptValidationRequest:
    exchange: str
    pair: str
    timeframe: str
    strategy: str
    train_timerange: str
    test_timerange: str
    run_id: str
    epochs: int
    random_state: int
    min_trades: int
    spaces: tuple[str, ...]
    hyperopt_loss: str
    job_workers: int
    exchange_check: bool
    feature_matrix: str | None = None
    skip_download: bool = False

    def __post_init__(self) -> None:
        if not self.exchange:
            raise ValueError("exchange is required")
        if not self.pair:
            raise ValueError("pair is required")
        if not self.timeframe:
            raise ValueError("timeframe is required")
        if not self.strategy:
            raise ValueError("strategy is required")
        if not self.run_id:
            raise ValueError("run_id is required")
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
        validate_feature_matrix_request(strategy=self.strategy, feature_matrix=self.feature_matrix)
        train_start, train_end = _parse_timerange(self.train_timerange)
        test_start, test_end = _parse_timerange(self.test_timerange)
        if train_start >= train_end or test_start >= test_end:
            raise ValueError("timerange start must be before end")
        if train_end > test_start:
            raise ValueError("train window must end before or at test window start")


def build_hyperopt_validation_plan(request: HyperoptValidationRequest) -> list[PipelineStep]:
    combined_timerange = _combined_timerange(request)
    steps = [
        PipelineStep(
            name="preflight",
            command=[
                "./ops/preflight.sh",
                "--research",
                *(
                    ["--exchange-check"]
                    if request.exchange_check and not request.skip_download
                    else []
                ),
            ],
        ),
    ]
    if not request.skip_download:
        steps.extend(
            (
                PipelineStep(
                    name="download_ohlcv",
                    command=_download_command(request, combined_timerange, prepend=False),
                ),
                PipelineStep(
                    name="backfill_ohlcv",
                    command=_download_command(request, combined_timerange, prepend=True),
                ),
            )
        )
    steps.extend(
        (
            PipelineStep(
                name="data_quality",
                command=_data_quality_command(request, combined_timerange),
            ),
            PipelineStep(
                name="ohlcv_integrity",
                command=_ohlcv_integrity_command(request, combined_timerange),
            ),
            PipelineStep(name="hyperopt", command=_hyperopt_command(request)),
            PipelineStep(name="hyperopt_show", command=_hyperopt_show_command(request)),
        )
    )
    return steps


def run_hyperopt_validation(*, request: HyperoptValidationRequest, root_dir: Path) -> Path:
    with freqtrade_state_lock(root_dir):
        return _run_hyperopt_validation_locked(request=request, root_dir=root_dir)


def _run_hyperopt_validation_locked(*, request: HyperoptValidationRequest, root_dir: Path) -> Path:
    source_feature_matrix = _validate_feature_matrix_path(
        root_dir=root_dir,
        feature_matrix=request.feature_matrix,
    )
    run_dir = root_dir / "user_data" / "research_runs" / request.run_id
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    execution_request = request
    feature_matrix_artifact = None
    if source_feature_matrix is not None:
        frozen_matrix = run_dir / "training_feature_matrix.parquet"
        shutil.copy2(source_feature_matrix, frozen_matrix)
        feature_matrix_artifact = _artifact(root_dir=root_dir, path=frozen_matrix)
        execution_request = replace(
            request,
            feature_matrix=frozen_matrix.relative_to(root_dir).as_posix(),
        )
    steps = build_hyperopt_validation_plan(execution_request)
    params_path = _strategy_params_path(root_dir=root_dir, strategy=request.strategy)
    original_params = params_path.read_bytes() if params_path.exists() else None

    _write_manifest(
        run_dir=run_dir,
        request=request,
        execution_request=execution_request,
        steps=steps,
        status="running",
        feature_matrix_artifact=feature_matrix_artifact,
    )
    try:
        for step in steps:
            _run_step(step=step, root_dir=root_dir, logs_dir=logs_dir)

        optimized_params = _archive_params(run_dir=run_dir, root_dir=root_dir, params_path=params_path)
        oos_run_dir = run_research_plan(
            request=BacktestRequest(
                exchange=execution_request.exchange,
                pair=execution_request.pair,
                timeframe=execution_request.timeframe,
                days=None,
                timerange=execution_request.test_timerange,
                strategy=execution_request.strategy,
                run_id=f"{execution_request.run_id}-oos",
                include_bias_checks=True,
                exchange_check=False,
                feature_matrix=execution_request.feature_matrix,
                skip_download=execution_request.skip_download,
            ),
            root_dir=root_dir,
        )
        readiness = json.loads((oos_run_dir / "readiness.json").read_text())
        result = {
            "optimized_params": optimized_params,
            "out_of_sample_readiness": readiness,
            "out_of_sample_run_dir": str(oos_run_dir),
            "passed": readiness.get("passed") is True,
            "request": asdict(request),
        }
        if feature_matrix_artifact is not None:
            result["feature_matrix_artifact"] = feature_matrix_artifact
        result_path = run_dir / "optimization_result.json"
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
        _write_manifest(
            run_dir=run_dir,
            request=request,
            execution_request=execution_request,
            steps=steps,
            status="completed",
            feature_matrix_artifact=feature_matrix_artifact,
        )
        return result_path
    except Exception:
        _write_manifest(
            run_dir=run_dir,
            request=request,
            execution_request=execution_request,
            steps=steps,
            status="failed",
            feature_matrix_artifact=feature_matrix_artifact,
        )
        raise
    finally:
        _restore_params(params_path=params_path, original_params=original_params)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run hyperopt on a train window and validate out of sample.")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--pair", default="BTC/USDT")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--strategy", default="BtcDonchianAtr")
    parser.add_argument("--train-timerange", required=True)
    parser.add_argument("--test-timerange", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--min-trades", type=int, default=50)
    parser.add_argument("--spaces", nargs="+")
    parser.add_argument("--hyperopt-loss", default="MultiMetricHyperOptLoss")
    parser.add_argument("--job-workers", type=int, default=1)
    parser.add_argument("--feature-matrix")
    parser.add_argument("--no-exchange-check", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    request = HyperoptValidationRequest(
        exchange=args.exchange,
        pair=args.pair,
        timeframe=args.timeframe,
        strategy=args.strategy,
        train_timerange=args.train_timerange,
        test_timerange=args.test_timerange,
        run_id=args.run_id,
        epochs=args.epochs,
        random_state=args.random_state,
        min_trades=args.min_trades,
        spaces=tuple(args.spaces) if args.spaces is not None else default_hyperopt_spaces(args.strategy),
        hyperopt_loss=args.hyperopt_loss,
        job_workers=args.job_workers,
        exchange_check=not args.no_exchange_check,
        feature_matrix=args.feature_matrix,
        skip_download=args.skip_download,
    )
    result_path = run_hyperopt_validation(request=request, root_dir=args.root_dir.resolve())
    print(result_path)
    result = json.loads(result_path.read_text())
    return 0 if result["passed"] else 1


def _download_command(
    request: HyperoptValidationRequest,
    timerange: str,
    *,
    prepend: bool,
) -> list[str]:
    command = [
        "docker",
        "compose",
        "run",
        *_docker_environment_args(_sota_candidate_environment(request.strategy)),
        "--rm",
        "freqtrade",
        "download-data",
        "--exchange",
        request.exchange,
        "--pairs",
        request.pair,
        "--timeframes",
        request.timeframe,
        "--timerange",
        timerange,
        "--trading-mode",
        "spot",
        "--data-format-ohlcv",
        "feather",
    ]
    if prepend:
        command.append("--prepend")
    return command


def _data_quality_command(request: HyperoptValidationRequest, timerange: str) -> list[str]:
    return [
        ".venv/bin/python",
        "-m",
        "btc_quant.data_quality",
        "--exchange",
        request.exchange,
        "--pair",
        request.pair,
        "--timeframe",
        request.timeframe,
        "--min-candles",
        str(_minimum_candles(timerange=timerange, timeframe=request.timeframe)),
        "--skip-staleness-check",
        "--output-dir",
        f"user_data/research_runs/{request.run_id}",
    ]


def _ohlcv_integrity_command(
    request: HyperoptValidationRequest,
    timerange: str,
) -> list[str]:
    return [
        ".venv/bin/python",
        "-m",
        "btc_quant.ohlcv_integrity",
        "--exchange",
        request.exchange,
        "--pair",
        request.pair,
        "--timeframe",
        request.timeframe,
        "--timerange",
        timerange,
        "--output-dir",
        f"user_data/research_runs/{request.run_id}",
    ]


def _hyperopt_command(request: HyperoptValidationRequest) -> list[str]:
    return [
        "docker",
        "compose",
        "run",
        *_docker_environment_args(_hyperopt_environment(request)),
        "--rm",
        "freqtrade",
        "hyperopt",
        "--config",
        CONFIG_PATH,
        "--strategy",
        request.strategy,
        "--timeframe",
        request.timeframe,
        "--pairs",
        request.pair,
        "--timerange",
        request.train_timerange,
        "--enable-protections",
        "--epochs",
        str(request.epochs),
        "--spaces",
        *request.spaces,
        "--random-state",
        str(request.random_state),
        "--min-trades",
        str(request.min_trades),
        "--hyperopt-loss",
        request.hyperopt_loss,
        "--job-workers",
        str(request.job_workers),
        "--analyze-per-epoch",
    ]


def _hyperopt_show_command(request: HyperoptValidationRequest) -> list[str]:
    return [
        "docker",
        "compose",
        "run",
        *_docker_environment_args(_sota_candidate_environment(request.strategy)),
        "--rm",
        "freqtrade",
        "hyperopt-show",
        "--config",
        CONFIG_PATH,
        "--best",
        "--print-json",
        "--breakdown",
        "day",
        "week",
        "month",
    ]


def _hyperopt_environment(request: HyperoptValidationRequest) -> list[str]:
    environment = _sota_candidate_environment(request.strategy)
    if request.feature_matrix is not None:
        environment.append(f"FREQTRADE_FEATURE_MATRIX={request.feature_matrix}")
    return environment


def _sota_candidate_environment(strategy: str) -> list[str]:
    return ["FREQTRADE_SOTA_CANDIDATE="] if strategy == "Sota" else []


def _docker_environment_args(environment: list[str]) -> list[str]:
    return [token for value in environment for token in ("-e", value)]


def _run_step(*, step: PipelineStep, root_dir: Path, logs_dir: Path) -> None:
    log_path = logs_dir / f"{step.name}.log"
    with log_path.open("w") as log_file:
        subprocess.run(
            step.command,
            cwd=root_dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=True,
            text=True,
        )


def _archive_params(*, run_dir: Path, root_dir: Path, params_path: Path) -> dict[str, Any]:
    if not params_path.exists():
        raise RuntimeError(f"hyperopt did not export strategy parameters at {params_path}")
    archive_path = run_dir / "optimized_params.json"
    shutil.copy2(params_path, archive_path)
    return _artifact(root_dir=root_dir, path=archive_path)


def _restore_params(*, params_path: Path, original_params: bytes | None) -> None:
    if original_params is None:
        params_path.unlink(missing_ok=True)
        return
    params_path.write_bytes(original_params)


def _write_manifest(
    *,
    run_dir: Path,
    request: HyperoptValidationRequest,
    execution_request: HyperoptValidationRequest,
    steps: list[PipelineStep],
    status: str,
    feature_matrix_artifact: dict[str, Any] | None,
) -> Path:
    payload = {
        "execution_request": asdict(execution_request),
        "request": asdict(request),
        "status": status,
        "steps": [asdict(step) for step in steps],
    }
    if feature_matrix_artifact is not None:
        payload["feature_matrix_artifact"] = feature_matrix_artifact
    manifest_path = run_dir / "optimization_manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return manifest_path


def _artifact(*, root_dir: Path, path: Path) -> dict[str, Any]:
    relative_path = path.relative_to(root_dir)
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


def _combined_timerange(request: HyperoptValidationRequest) -> str:
    train_start, _ = _parse_timerange(request.train_timerange)
    _, test_end = _parse_timerange(request.test_timerange)
    return f"{train_start:%Y%m%d}-{test_end:%Y%m%d}"


def _minimum_candles(*, timerange: str, timeframe: str) -> int:
    start, end = _parse_timerange(timerange)
    candles_per_day = 86_400 / timeframe_to_seconds(timeframe)
    return max(1, int((end - start).days * candles_per_day * 0.94))


def validate_feature_matrix_request(*, strategy: str, feature_matrix: str | None) -> None:
    if feature_matrix is not None and not feature_matrix:
        raise ValueError("feature_matrix cannot be blank")
    if feature_matrix is not None and Path(feature_matrix).is_absolute():
        raise ValueError("feature_matrix must be relative to repo root")
    if strategy in FEATURE_MATRIX_STRATEGIES and feature_matrix is None:
        raise ValueError(f"{strategy} requires feature_matrix")
    if feature_matrix is not None and strategy not in FEATURE_MATRIX_STRATEGIES:
        raise ValueError("feature_matrix requires strategy BtcMultiSourceRegimeStrategy or Sota")


def default_hyperopt_spaces(strategy: str) -> tuple[str, ...]:
    return ("buy",) if strategy == "Sota" else ("buy", "sell")


def _validate_feature_matrix_path(*, root_dir: Path, feature_matrix: str | None) -> Path | None:
    if feature_matrix is None:
        return None
    resolved_root_dir = root_dir.resolve()
    resolved_feature_matrix = (resolved_root_dir / feature_matrix).resolve()
    try:
        resolved_feature_matrix.relative_to(resolved_root_dir)
    except ValueError as error:
        raise ValueError("feature_matrix must resolve under repo root") from error
    if not resolved_feature_matrix.is_file():
        raise ValueError(f"missing feature matrix: {feature_matrix}")
    return resolved_feature_matrix


def _strategy_params_path(*, root_dir: Path, strategy: str) -> Path:
    source_name = STRATEGY_SOURCE_FILES.get(strategy, f"{strategy}.py")
    return root_dir / "user_data" / "strategies" / Path(source_name).with_suffix(".json")


def _parse_timerange(timerange: str) -> tuple[datetime, datetime]:
    start_text, end_text = timerange.split("-", maxsplit=1)
    return datetime.strptime(start_text, "%Y%m%d"), datetime.strptime(end_text, "%Y%m%d")


if __name__ == "__main__":
    raise SystemExit(main())

