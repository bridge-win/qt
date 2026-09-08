# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.freqtrade_state_lock import freqtrade_state_lock
from qt.legacy.btc_quant_evolution.ohlcv_integrity import data_file_path, timeframe_to_seconds
from qt.legacy.btc_quant_evolution.readiness import write_readiness_report
from qt.legacy.btc_quant_evolution.regime import write_regime_report
from qt.legacy.btc_quant_evolution.reports import write_backtest_summary
from qt.legacy.btc_quant_evolution.robustness import RobustnessPolicy, write_robustness_report
from qt.legacy.btc_quant_evolution.strategy_catalog import (
    RUNTIME_CATALOG_PATH,
    build_catalog,
    load_runtime_catalog,
    profile_by_id,
    write_runtime_catalog,
)
from qt.legacy.btc_quant_evolution.strategy_provenance import sota_runtime_source_paths

CONFIG_PATH = "/freqtrade/user_data/config.json"
DEFAULT_BACKTEST_DIR = "/freqtrade/user_data/backtest_results"
DEFAULT_RESEARCH_DAYS = 3650
FEATURE_MATRIX_STRATEGIES = frozenset({"BtcMultiSourceRegimeStrategy", "Sota"})
STRATEGY_SOURCE_FILES = {"Sota": "sota.py"}


@dataclass(frozen=True)
class BacktestRequest:
    exchange: str
    pair: str
    timeframe: str
    days: int | None
    timerange: str | None
    strategy: str
    run_id: str
    include_bias_checks: bool
    exchange_check: bool
    profile: str | None = None
    feature_matrix: str | None = None
    candidate_config: str | None = None
    fee_bps_per_side: float = 0.0
    slippage_bps_per_side: float = 10.0
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
        if self.profile is not None and not self.profile:
            raise ValueError("profile cannot be blank")
        if self.profile is not None and self.strategy != "BtcCatalogStrategy":
            raise ValueError("profile requires strategy BtcCatalogStrategy")
        if self.strategy == "BtcCatalogStrategy" and self.profile is None:
            raise ValueError("BtcCatalogStrategy requires profile")
        if self.feature_matrix is not None and not self.feature_matrix:
            raise ValueError("feature_matrix cannot be blank")
        if self.feature_matrix is not None and Path(self.feature_matrix).is_absolute():
            raise ValueError("feature_matrix must be relative to repo root")
        if self.strategy in FEATURE_MATRIX_STRATEGIES and self.feature_matrix is None:
            raise ValueError(f"{self.strategy} requires feature_matrix")
        if self.feature_matrix is not None and self.strategy not in FEATURE_MATRIX_STRATEGIES:
            raise ValueError("feature_matrix requires strategy BtcMultiSourceRegimeStrategy or Sota")
        if self.candidate_config is not None and not self.candidate_config:
            raise ValueError("candidate_config cannot be blank")
        if self.candidate_config is not None and Path(self.candidate_config).is_absolute():
            raise ValueError("candidate_config must be relative to repo root")
        if self.candidate_config is not None and self.strategy != "BtcMultiSourceRegimeStrategy":
            raise ValueError("candidate_config requires strategy BtcMultiSourceRegimeStrategy")
        if (self.days is None) == (self.timerange is None):
            raise ValueError("exactly one of days or timerange is required")
        if self.days is not None and self.days <= 0:
            raise ValueError("days must be positive")
        if self.fee_bps_per_side < 0:
            raise ValueError("fee_bps_per_side cannot be negative")
        if self.slippage_bps_per_side < 0:
            raise ValueError("slippage_bps_per_side cannot be negative")


@dataclass(frozen=True)
class PipelineStep:
    name: str
    command: list[str]


def build_research_plan(request: BacktestRequest, *, as_of_date: date | None = None) -> list[PipelineStep]:
    analysis_timerange = _analysis_timerange(request, as_of_date=as_of_date)
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
                PipelineStep(name="download_ohlcv", command=_download_command(request, prepend=False)),
                PipelineStep(name="backfill_ohlcv", command=_download_command(request, prepend=True)),
            )
        )
    steps.extend(
        (
            PipelineStep(name="data_quality", command=_data_quality_command(request)),
            PipelineStep(
                name="ohlcv_integrity",
                command=_ohlcv_integrity_command(
                    request,
                    analysis_timerange=analysis_timerange,
                ),
            ),
        )
    )
    if request.feature_matrix is not None:
        steps.append(
            PipelineStep(
                name="feature_matrix_quality",
                command=_feature_matrix_quality_command(request, analysis_timerange=analysis_timerange),
            )
        )
    steps.append(PipelineStep(name="backtest", command=_backtest_command(request, analysis_timerange=analysis_timerange)))

    if request.include_bias_checks:
        steps.extend(
            [
                PipelineStep(
                    name="lookahead_analysis",
                    command=_lookahead_command(request, analysis_timerange=analysis_timerange),
                ),
                PipelineStep(
                    name="recursive_analysis",
                    command=_recursive_command(request, analysis_timerange=analysis_timerange),
                ),
            ]
        )

    return steps


def write_manifest(
    *,
    output_dir: Path,
    request: BacktestRequest,
    steps: list[PipelineStep],
    status: str,
    started_at: str,
    completed_at: str | None,
    git_revision: str,
    artifacts: dict[str, Any] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "request": asdict(request),
        "steps": [asdict(step) for step in steps],
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "git_revision": git_revision,
    }
    if artifacts is not None:
        manifest["artifacts"] = artifacts
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return manifest_path


def build_artifact_manifest(*, root_dir: Path, request: BacktestRequest) -> dict[str, dict[str, Any]]:
    artifact_paths = {
        "config": root_dir / "user_data" / "config.json",
        "ohlcv_data": data_file_path(
            root_dir=root_dir,
            exchange=request.exchange,
            pair=request.pair,
            timeframe=request.timeframe,
        ),
        "strategy": _strategy_source_path(root_dir, request.strategy),
    }
    artifacts: dict[str, dict[str, Any]] = {
        name: _file_artifact(root_dir=root_dir, path=path)
        for name, path in artifact_paths.items()
    }
    if request.strategy == "Sota":
        artifacts["strategy_runtime_sources"] = {
            name: _file_artifact(root_dir=root_dir, path=path)
            for name, path in sota_runtime_source_paths(root_dir).items()
        }
    if request.profile is not None:
        catalog_path = root_dir / RUNTIME_CATALOG_PATH
        artifacts["catalog"] = _file_artifact(root_dir=root_dir, path=catalog_path)
        artifacts["catalog_strategy"] = _file_artifact(
            root_dir=root_dir,
            path=root_dir / "user_data" / "strategies" / "BtcCatalogStrategy.py",
        )
        if catalog_path.exists():
            payload = load_runtime_catalog(catalog_path)
            artifacts["catalog_profile"] = _runtime_profile_artifact(payload, request.profile)
        else:
            artifacts["catalog_profile"] = {"exists": False, "id": request.profile}
    if request.feature_matrix is not None:
        artifacts["feature_matrix"] = _file_artifact(
            root_dir=root_dir,
            path=root_dir / request.feature_matrix,
        )
    if request.candidate_config is not None:
        artifacts["candidate_config"] = _file_artifact(
            root_dir=root_dir,
            path=root_dir / request.candidate_config,
        )
    return artifacts


def run_research_plan(*, request: BacktestRequest, root_dir: Path) -> Path:
    with freqtrade_state_lock(root_dir):
        return _run_research_plan_locked(request=request, root_dir=root_dir)


def _run_research_plan_locked(*, request: BacktestRequest, root_dir: Path) -> Path:
    _validate_feature_matrix(root_dir=root_dir, request=request)
    _validate_candidate_config(root_dir=root_dir, request=request)
    if request.profile is not None:
        write_runtime_catalog(root_dir=root_dir)
    run_dir = root_dir / "user_data" / "research_runs" / request.run_id
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    steps = build_research_plan(request)
    started_at = _utc_now()
    git_revision = _git_revision(root_dir)
    write_manifest(
        output_dir=run_dir,
        request=request,
        steps=steps,
        status="running",
        started_at=started_at,
        completed_at=None,
        git_revision=git_revision,
        artifacts=build_artifact_manifest(root_dir=root_dir, request=request),
    )

    try:
        for step in steps:
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
            if has_fatal_freqtrade_error(log_path.read_text()):
                raise RuntimeError(f"{step.name} emitted a fatal freqtrade error; see {log_path}")
    except subprocess.CalledProcessError:
        write_manifest(
            output_dir=run_dir,
            request=request,
            steps=steps,
            status="failed",
            started_at=started_at,
            completed_at=_utc_now(),
            git_revision=git_revision,
            artifacts=build_artifact_manifest(root_dir=root_dir, request=request),
        )
        raise
    except RuntimeError:
        write_manifest(
            output_dir=run_dir,
            request=request,
            steps=steps,
            status="failed",
            started_at=started_at,
            completed_at=_utc_now(),
            git_revision=git_revision,
            artifacts=build_artifact_manifest(root_dir=root_dir, request=request),
        )
        raise

    write_backtest_summary(
        run_dir=run_dir,
        backtest_dir=root_dir / "user_data" / "backtest_results",
        strategy=request.strategy,
    )
    write_robustness_report(
        run_dir=run_dir,
        backtest_dir=root_dir / "user_data" / "backtest_results",
        strategy=request.strategy,
        policy=RobustnessPolicy(
            fee_bps_per_side=request.fee_bps_per_side,
            slippage_bps=request.slippage_bps_per_side,
        ),
    )
    write_regime_report(
        run_dir=run_dir,
        root_dir=root_dir,
        backtest_dir=root_dir / "user_data" / "backtest_results",
        exchange=request.exchange,
        pair=request.pair,
        timeframe=request.timeframe,
        strategy=request.strategy,
    )
    write_manifest(
        output_dir=run_dir,
        request=request,
        steps=steps,
        status="completed",
        started_at=started_at,
        completed_at=_utc_now(),
        git_revision=git_revision,
        artifacts=build_artifact_manifest(root_dir=root_dir, request=request),
    )
    write_readiness_report(run_dir=run_dir)
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the BTC quant research pipeline.")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--pair", default="BTC/USDT")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--days", type=int, default=DEFAULT_RESEARCH_DAYS)
    parser.add_argument("--timerange")
    parser.add_argument("--strategy", default="BtcDonchianAtr")
    parser.add_argument("--profile")
    parser.add_argument("--feature-matrix")
    parser.add_argument("--run-id", default=_default_run_id())
    parser.add_argument("--skip-bias-checks", action="store_true")
    parser.add_argument("--no-exchange-check", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--fee-bps-per-side", type=float, default=0.0)
    parser.add_argument("--slippage-bps-per-side", type=float, default=10.0)
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    days = None if args.timerange else args.days
    request = BacktestRequest(
        exchange=args.exchange,
        pair=args.pair,
        timeframe=args.timeframe,
        days=days,
        timerange=args.timerange,
        strategy=args.strategy,
        run_id=args.run_id,
        include_bias_checks=not args.skip_bias_checks,
        exchange_check=not args.no_exchange_check,
        profile=args.profile,
        feature_matrix=args.feature_matrix,
        fee_bps_per_side=args.fee_bps_per_side,
        slippage_bps_per_side=args.slippage_bps_per_side,
        skip_download=args.skip_download,
    )
    run_dir = run_research_plan(request=request, root_dir=args.root_dir.resolve())
    print(f"research_run={run_dir}")
    readiness = json.loads((run_dir / "readiness.json").read_text())
    return 0 if readiness.get("passed") is True else 1


def _download_command(request: BacktestRequest, *, prepend: bool) -> list[str]:
    command = [
        *_freqtrade_command("download-data", env_overrides=_freqtrade_env(request)),
        "--exchange",
        request.exchange,
        "--pairs",
        request.pair,
        "--timeframes",
        request.timeframe,
        *_history_window_args(request),
        "--trading-mode",
        "spot",
        "--data-format-ohlcv",
        "feather",
    ]
    if prepend:
        command.append("--prepend")
    return command


def _backtest_command(request: BacktestRequest, *, analysis_timerange: str) -> list[str]:
    return [
        *_freqtrade_command("backtesting", env_overrides=_freqtrade_env(request)),
        "--config",
        CONFIG_PATH,
        "--strategy",
        request.strategy,
        "--timeframe",
        request.timeframe,
        "--pairs",
        request.pair,
        "--timerange",
        analysis_timerange,
        "--enable-protections",
        "--cache",
        "none",
        "--export",
        "trades",
        "--breakdown",
        "day",
        "week",
        "month",
        "--notes",
        _research_notes(request),
    ]


def _ohlcv_integrity_command(
    request: BacktestRequest,
    *,
    analysis_timerange: str,
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
        analysis_timerange,
        "--output-dir",
        f"user_data/research_runs/{request.run_id}",
    ]


def _feature_matrix_quality_command(request: BacktestRequest, *, analysis_timerange: str) -> list[str]:
    if request.feature_matrix is None:
        raise ValueError("feature matrix quality command requires feature_matrix")
    return [
        ".venv/bin/python",
        "-m",
        "btc_quant.features.feature_matrix_quality",
        "--matrix",
        request.feature_matrix,
        "--exchange",
        request.exchange,
        "--pair",
        request.pair,
        "--timeframe",
        request.timeframe,
        "--timerange",
        analysis_timerange,
        "--allow-matrix-superset",
        "--output-dir",
        f"user_data/research_runs/{request.run_id}",
    ]


def _data_quality_command(request: BacktestRequest) -> list[str]:
    command = [
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
        str(_minimum_candles(request)),
    ]
    if request.timerange is None:
        command.extend(["--max-staleness-hours", "48"])
    else:
        command.append("--skip-staleness-check")
    command.extend(["--output-dir", f"user_data/research_runs/{request.run_id}"])
    return command


def _lookahead_command(request: BacktestRequest, *, analysis_timerange: str) -> list[str]:
    return [
        *_freqtrade_command(
            "lookahead-analysis",
            env_overrides=[
                *_freqtrade_env(request),
                "FREQTRADE__ENTRY_PRICING__PRICE_SIDE=other",
                "FREQTRADE__EXIT_PRICING__PRICE_SIDE=other",
            ],
        ),
        "--config",
        CONFIG_PATH,
        "--strategy",
        request.strategy,
        "--timeframe",
        request.timeframe,
        "--pairs",
        request.pair,
        "--timerange",
        analysis_timerange,
        "--enable-protections",
        "--minimum-trade-amount",
        "5",
        "--targeted-trade-amount",
        "20",
    ]


def _recursive_command(request: BacktestRequest, *, analysis_timerange: str) -> list[str]:
    return [
        *_freqtrade_command("recursive-analysis", env_overrides=_freqtrade_env(request)),
        "--config",
        CONFIG_PATH,
        "--strategy",
        request.strategy,
        "--timeframe",
        request.timeframe,
        "--pairs",
        request.pair,
        "--timerange",
        analysis_timerange,
        "--startup-candle",
        *_recursive_startup_candles(request.strategy),
    ]


def _recursive_startup_candles(strategy: str) -> list[str]:
    if strategy == "Sota":
        return ["2161", "2200", "2500"]
    return ["120", "240", "480"]


def _freqtrade_command(subcommand: str, *, env_overrides: list[str] | None = None) -> list[str]:
    command = ["docker", "compose", "run"]
    for env_override in env_overrides or []:
        command.extend(["-e", env_override])
    command.extend(["--rm", "freqtrade", subcommand])
    return command


def _freqtrade_env(request: BacktestRequest) -> list[str]:
    environment: list[str] = []
    if request.profile is not None:
        environment.append(f"FREQTRADE_PROFILE={request.profile}")
    return [
        *environment,
        *_feature_matrix_env(request),
        *_candidate_config_env(request),
        *_sota_candidate_env(request),
    ]


def _feature_matrix_env(request: BacktestRequest) -> list[str]:
    if request.feature_matrix is not None:
        return [f"FREQTRADE_FEATURE_MATRIX={request.feature_matrix}"]
    return ["FREQTRADE_FEATURE_MATRIX="]


def _candidate_config_env(request: BacktestRequest) -> list[str]:
    if request.candidate_config is not None:
        return [f"FREQTRADE_EVOLUTION_CANDIDATE={request.candidate_config}"]
    return []


def _sota_candidate_env(request: BacktestRequest) -> list[str]:
    return ["FREQTRADE_SOTA_CANDIDATE="] if request.strategy == "Sota" else []


def _validate_feature_matrix(*, root_dir: Path, request: BacktestRequest) -> None:
    if request.feature_matrix is None:
        return
    if not (root_dir / request.feature_matrix).is_file():
        raise ValueError(f"missing feature matrix: {request.feature_matrix}")


def _validate_candidate_config(*, root_dir: Path, request: BacktestRequest) -> None:
    if request.candidate_config is None:
        return
    if not (root_dir / request.candidate_config).is_file():
        raise ValueError(f"missing candidate config: {request.candidate_config}")


def _research_notes(request: BacktestRequest) -> str:
    if request.profile is None:
        return f"research_run={request.run_id}"
    return f"research_run={request.run_id} profile={request.profile}"


def _history_window_args(request: BacktestRequest) -> list[str]:
    if request.timerange is not None:
        return ["--timerange", request.timerange]
    if request.days is not None:
        return ["--days", str(request.days)]
    raise ValueError("exactly one of days or timerange is required")


def _analysis_timerange(request: BacktestRequest, *, as_of_date: date | None) -> str:
    if request.timerange is not None:
        return request.timerange
    if request.days is None:
        raise ValueError("exactly one of days or timerange is required")

    end_date = as_of_date or datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=request.days)
    return f"{start_date:%Y%m%d}-{end_date:%Y%m%d}"


def _minimum_candles(request: BacktestRequest) -> int:
    window_days = request.days if request.days is not None else _timerange_days(request.timerange)
    candles_per_day = 86_400 / timeframe_to_seconds(request.timeframe)
    return max(1, int(window_days * candles_per_day * 0.94))


def _timerange_days(timerange: str | None) -> int:
    if timerange is None:
        raise ValueError("exactly one of days or timerange is required")
    start_text, end_text = timerange.split("-", maxsplit=1)
    start = datetime.strptime(start_text, "%Y%m%d").date()
    end = datetime.strptime(end_text, "%Y%m%d").date()
    if start >= end:
        raise ValueError("timerange start must be before end")
    return (end - start).days


def has_fatal_freqtrade_error(log_text: str) -> bool:
    return " - freqtrade - ERROR - " in log_text


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git_revision(root_dir: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else "no-commits"


def _file_artifact(*, root_dir: Path, path: Path) -> dict[str, Any]:
    relative_path = path.relative_to(root_dir)
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


def _runtime_profile_artifact(payload: dict[str, Any], profile_id: str) -> dict[str, Any]:
    profile = next((item for item in payload["profiles"] if item["id"] == profile_id), None)
    if profile is None:
        raise ValueError(f"unknown catalog profile: {profile_id}")
    source_profile = profile_by_id(build_catalog(), profile_id)
    return {
        "exists": True,
        "id": profile_id,
        "family": profile["family"],
        "version": profile["version"],
        "catalog_sha256": payload["sha256"],
        "definition_sha256": hashlib.sha256(
            json.dumps(
                asdict(source_profile),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strategy_source_path(root_dir: Path, strategy: str) -> Path:
    filename = STRATEGY_SOURCE_FILES.get(strategy, f"{strategy}.py")
    return root_dir / "user_data" / "strategies" / filename


if __name__ == "__main__":
    raise SystemExit(main())

