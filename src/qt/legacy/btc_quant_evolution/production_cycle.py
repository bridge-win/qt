# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

from qt.legacy.btc_quant_evolution.candidate_package import SOTA_MIN_ROLLING_SPLITS, write_sota_candidate_package
from qt.legacy.btc_quant_evolution.evolution.candidate_space import default_candidate_space
from qt.legacy.btc_quant_evolution.evolution.promotion_gate import PromotionPolicy, evaluate_candidate_promotion
from qt.legacy.btc_quant_evolution.evolution.walk_forward_evolution import build_evolution_split_plan, run_evolution
from qt.legacy.btc_quant_evolution.external_data.registry import provider_selection_fingerprint
from qt.legacy.btc_quant_evolution.external_data.schemas import ProviderSyncRequest
from qt.legacy.btc_quant_evolution.external_data.sync import sync_external_data
from qt.legacy.btc_quant_evolution.features.feature_matrix import build_feature_matrix
from qt.legacy.btc_quant_evolution.features.feature_matrix_quality import (
    validate_feature_matrix_window as evaluate_feature_matrix_quality,
)
from qt.legacy.btc_quant_evolution.hyperopt_walk_forward import (
    HyperoptWalkForwardRequest,
    run_hyperopt_walk_forward_validation,
    validate_hyperopt_walk_forward_evidence,
)
from qt.legacy.btc_quant_evolution.ohlcv_integrity import data_file_path
from qt.legacy.btc_quant_evolution.production_launch import (
    CommandResult,
    CommandRunner,
    LaunchBlockedError,
    SubprocessCommandRunner,
    launch_dry_run,
    launch_live,
    validate_candidate_package_for_launch,
    validate_live_readiness_evidence,
)
from qt.legacy.btc_quant_evolution.production_manifest import (
    PHASES,
    PRODUCTION_STRATEGY_TIMEFRAMES,
    ProductionManifestStore,
    ProductionRunConfig,
    run_lock,
)
from qt.legacy.btc_quant_evolution.research_pipeline import BacktestRequest, run_research_plan
from qt.legacy.btc_quant_evolution.runtime_evidence import dry_run_receipt_path, load_dry_run_manifest_anchor


@dataclass(frozen=True)
class PhaseExecution:
    outputs: tuple[Path, ...]
    details: dict[str, object]


@dataclass(frozen=True)
class PhaseContext:
    config: ProductionRunConfig
    env: Mapping[str, str]
    runner: CommandRunner
    store: ProductionManifestStore

    @property
    def root_dir(self) -> Path:
        return self.config.root_dir.resolve()

    @property
    def run_dir(self) -> Path:
        return self.store.run_dir

    @property
    def artifacts_dir(self) -> Path:
        return self.run_dir / "artifacts"

    def phase_outputs(self, phase: str) -> tuple[Path, ...]:
        manifest = json.loads(self.store.manifest_path.read_text())
        phases = manifest.get("phases")
        record = phases.get(phase) if isinstance(phases, dict) else None
        outputs = record.get("outputs") if isinstance(record, dict) else None
        if not isinstance(outputs, list):
            return ()
        paths: list[Path] = []
        for artifact in outputs:
            path_text = artifact.get("path") if isinstance(artifact, dict) else None
            if isinstance(path_text, str):
                paths.append(self.root_dir / path_text)
        return tuple(paths)

    def require_phase_output(self, phase: str, name: str) -> Path:
        matches = [path for path in self.phase_outputs(phase) if path.name == name]
        if len(matches) != 1 or not matches[0].is_file():
            raise PhaseGateError(f"{phase} did not produce {name}")
        return matches[0]


class PhaseGateError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
        outputs: Sequence[Path] = (),
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})
        self.outputs = tuple(outputs)


PhaseExecutor = Callable[[PhaseContext], PhaseExecution]


def run_production_cycle(
    config: ProductionRunConfig,
    env: Mapping[str, str],
    runner: CommandRunner,
) -> Path:
    effective_config = replace(
        config,
        provider_fingerprint=provider_selection_fingerprint(env, config.source_policy),
    )
    _validate_config(effective_config)
    store = ProductionManifestStore(effective_config)
    with run_lock(store.run_dir):
        if store.manifest_path.exists():
            store.load()
        else:
            store = ProductionManifestStore.create(effective_config)
            store.load()

        context = PhaseContext(config=effective_config, env=env, runner=runner, store=store)
        target_index = PHASES.index(effective_config.through)
        for phase in PHASES[: target_index + 1]:
            if _phase_reusable(context, phase):
                continue
            if phase == "live":
                try:
                    _ingest_live_readiness(context)
                except LaunchBlockedError as exc:
                    store.start_phase(phase)
                    reason = _redact(str(exc), env)
                    phase_report = _write_phase_report(
                        context,
                        phase,
                        status="blocked",
                        details={"reason": reason},
                        outputs=(),
                    )
                    store.blocked_phase(phase, reason, (phase_report,))
                    break
            store.start_phase(phase)
            try:
                execution = _PHASE_EXECUTORS[phase](context)
                phase_report = _write_phase_report(
                    context,
                    phase,
                    status="passed",
                    details=execution.details,
                    outputs=execution.outputs,
                )
                store.complete_phase(phase, (*execution.outputs, phase_report))
            except LaunchBlockedError as exc:
                reason = _redact(str(exc), env)
                phase_report = _write_phase_report(
                    context,
                    phase,
                    status="blocked",
                    details={"reason": reason},
                    outputs=(),
                )
                store.blocked_phase(phase, reason, (phase_report,))
                break
            except PhaseGateError as exc:
                reason = _redact(str(exc), env)
                phase_report = _write_phase_report(
                    context,
                    phase,
                    status="failed",
                    details={**exc.details, "reason": reason},
                    outputs=exc.outputs,
                )
                store.fail_phase(phase, reason, (*exc.outputs, phase_report))
                break
            except Exception as exc:
                reason = _redact(str(exc), env) or exc.__class__.__name__
                phase_report = _write_phase_report(
                    context,
                    phase,
                    status="failed",
                    details={"reason": reason},
                    outputs=(),
                )
                store.fail_phase(phase, reason, (phase_report,))
                break
    return store.manifest_path


def _phase_reusable(context: PhaseContext, phase: str) -> bool:
    if not context.store.reusable_phase(phase):
        return False
    if phase == "dry-run" and context.config.strategy == "Sota":
        package_matches = [
            path
            for path in context.phase_outputs("evolve")
            if path.name == "research_candidate_package.json"
        ]
        if len(package_matches) != 1:
            return False
        try:
            package = json.loads(package_matches[0].read_text())
        except (OSError, json.JSONDecodeError):
            return False
        if isinstance(package, dict) and package.get("schema_version") == 2:
            return True
        receipt_matches = [
            path
            for path in context.phase_outputs("dry-run")
            if path.name == "dry_run_launch_receipt.json"
        ]
        if len(receipt_matches) != 1:
            return False
        try:
            receipt = json.loads(receipt_matches[0].read_text())
            if not isinstance(receipt, dict):
                return False
            load_dry_run_manifest_anchor(
                root_dir=context.root_dir,
                candidate_package_path=package_matches[0],
                receipt_path=receipt_matches[0],
                receipt=receipt,
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return True
    if phase != "evolve" or context.config.strategy != "Sota":
        return True
    package_matches = [
        path
        for path in context.phase_outputs("evolve")
        if path.name == "research_candidate_package.json"
    ]
    if not package_matches:
        return True
    package_path = package_matches[0]
    try:
        package = json.loads(package_path.read_text())
    except (OSError, json.JSONDecodeError):
        package_path.unlink(missing_ok=True)
        return False
    if isinstance(package, dict) and package.get("schema_version") == 2:
        return True
    matrix_matches = [
        path
        for path in context.phase_outputs("matrix")
        if path.name == "feature_matrix.parquet"
    ]
    if len(matrix_matches) != 1:
        package_path.unlink(missing_ok=True)
        return False
    try:
        validate_candidate_package_for_launch(
            root_dir=context.root_dir,
            candidate_package_path=package_path,
            matrix_path=matrix_matches[0],
        )
    except (LaunchBlockedError, OSError, ValueError, json.JSONDecodeError):
        package_path.unlink(missing_ok=True)
        return False
    return True


def evaluate_research_qualification(candidate: Mapping[str, object]) -> dict[str, object]:
    reports = _candidate_reports(candidate)
    sanitized_reports = copy.deepcopy(reports)
    final_oos = sanitized_reports.get("final_oos")
    if isinstance(final_oos, dict):
        final_oos.pop("dry_run", None)
        final_oos.pop("operational", None)

    evaluation_reports = copy.deepcopy(sanitized_reports)
    evaluation_final_oos = evaluation_reports.get("final_oos")
    if isinstance(evaluation_final_oos, dict):
        evaluation_final_oos["dry_run"] = {"passed": True}
        evaluation_final_oos["operational"] = {"passed": True}

    candidate_id = candidate.get("candidate_id")
    evaluated = evaluate_candidate_promotion(
        str(candidate_id) if candidate_id is not None else "unknown",
        evaluation_reports,
        PromotionPolicy(),
    )
    evaluated["reports"] = sanitized_reports
    evaluated["stage"] = "research-qualified"
    return evaluated


def write_research_candidate_package(
    *,
    candidate: Mapping[str, object],
    evolution_result_path: Path,
    output_path: Path,
    root_dir: Path,
) -> Path:
    qualification = evaluate_research_qualification(candidate)
    if qualification.get("passed") is not True:
        raise ValueError("candidate is not research-qualified")
    candidate_id = candidate.get("candidate_id")
    evolution_result = _read_json_object(evolution_result_path, "evolution result")
    selection = evolution_result.get("selection")
    if (
        evolution_result.get("schema_version") != 2
        or evolution_result.get("final_oos_evaluation_count") != 1
        or not isinstance(selection, dict)
        or selection.get("selected_candidate_id") != candidate_id
        or evolution_result.get("selected_candidate") != candidate
    ):
        raise ValueError("candidate does not match the frozen evolution selection")
    split_plan = evolution_result.get("split_plan")
    if not isinstance(split_plan, list) or len(split_plan) != 3:
        raise ValueError("evolution result split plan is incomplete")
    candidate_config_artifact = _verified_candidate_config_artifact(candidate, root_dir)
    package = {
        "candidate": candidate.get("candidate"),
        "candidate_config_artifact": candidate_config_artifact,
        "candidate_id": candidate_id,
        "evolution_result_artifact": _artifact_record(root_dir, evolution_result_path),
        "metrics": candidate.get("metrics"),
        "passed": True,
        "qualification": qualification,
        "selection": selection,
        "schema_version": 2,
        "split_plan": split_plan,
        "stage": "research-qualified",
    }
    _write_json_atomic(output_path, package)
    return output_path


def _execute_collect(context: PhaseContext) -> PhaseExecution:
    requested_start = date.fromisoformat(context.config.start)
    request = ProviderSyncRequest(
        symbol=_provider_symbol(context.config.symbol),
        timeframe=context.config.timeframe,
        start=requested_start - timedelta(days=1),
        end=date.fromisoformat(context.config.end),
        source_policy=context.config.source_policy,
    )
    manifest_dir = sync_external_data(
        context.root_dir,
        request,
        context.config.run_id,
        context.env,
    )
    summary_path = manifest_dir / "sync-summary.json"
    summary = _read_json_object(summary_path, "external data sync summary")
    if summary.get("passed") is not True:
        raise PhaseGateError(_failed_reasons(summary, "external data collection failed"))
    sync_artifact_paths = _sync_artifact_paths(context.root_dir, summary)
    parquet_paths = _successful_parquet_paths(context.root_dir, summary, context.config.timeframe)
    if not parquet_paths:
        raise PhaseGateError("external data collection produced no successful provider artifacts")
    return PhaseExecution(
        outputs=(summary_path, *sync_artifact_paths),
        details={
            "collection_window": context.config.execution_plan()["collection_window"],
            "source_policy": context.config.source_policy,
            "successful_artifact_count": len(parquet_paths),
            "sync_summary": summary_path.relative_to(context.root_dir).as_posix(),
        },
    )


def _execute_matrix(context: PhaseContext) -> PhaseExecution:
    summary_path = context.require_phase_output("collect", "sync-summary.json")
    summary = _read_json_object(summary_path, "external data sync summary")
    if summary.get("passed") is not True:
        raise PhaseGateError("required-domain summary did not pass")
    external_paths = _successful_parquet_paths(context.root_dir, summary, context.config.timeframe)
    output_path = context.artifacts_dir / "feature_matrix.parquet"
    quality_path = context.artifacts_dir / "feature_matrix_quality.json"
    output_path.unlink(missing_ok=True)
    quality_path.unlink(missing_ok=True)
    matrix_path = build_feature_matrix(
        root_dir=context.root_dir,
        exchange="binance",
        pair=context.config.symbol,
        timeframe=context.config.timeframe,
        external_paths=external_paths,
        output=output_path,
        start=context.config.start,
        end=context.config.end,
    )
    quality = evaluate_feature_matrix_quality(
        matrix_path=matrix_path,
        ohlcv_path=data_file_path(
            root_dir=context.root_dir,
            exchange="binance",
            pair=context.config.symbol,
            timeframe=context.config.timeframe,
        ),
        timeframe=context.config.timeframe,
        timerange=_timerange(context.config),
    )
    _write_json_atomic(quality_path, quality)
    if quality.get("passed") is not True:
        raise PhaseGateError(_failed_reasons(quality, "feature matrix quality failed"))
    return PhaseExecution(
        outputs=(matrix_path, quality_path),
        details={"feature_matrix": matrix_path.relative_to(context.root_dir).as_posix(), "quality": quality},
    )


def _execute_research(context: PhaseContext) -> PhaseExecution:
    matrix_path = context.require_phase_output("matrix", "feature_matrix.parquet")
    request = BacktestRequest(
        exchange="binance",
        pair=context.config.symbol,
        timeframe=context.config.timeframe,
        days=None,
        timerange=_timerange(context.config),
        strategy=context.config.strategy,
        run_id=f"{context.config.run_id}-research",
        include_bias_checks=True,
        exchange_check=False,
        feature_matrix=matrix_path.relative_to(context.root_dir).as_posix(),
        skip_download=True,
    )
    research_dir = run_research_plan(request=request, root_dir=context.root_dir)
    readiness_path = research_dir / "readiness.json"
    readiness = _read_json_object(readiness_path, "research readiness")
    baseline_qualified = readiness.get("passed")
    if not isinstance(baseline_qualified, bool):
        raise PhaseGateError("research readiness has no boolean passed result")
    failed_reasons = readiness.get("failed_reasons")
    if not isinstance(failed_reasons, list):
        failed_reasons = [] if baseline_qualified else ["research readiness failed"]
    outputs = tuple(path for path in sorted(research_dir.rglob("*")) if path.is_file())
    return PhaseExecution(
        outputs=outputs,
        details={
            "baseline_qualified": baseline_qualified,
            "failed_reasons": [str(reason) for reason in failed_reasons],
            "research_run": research_dir.relative_to(context.root_dir).as_posix(),
        },
    )


def _execute_evolve(context: PhaseContext) -> PhaseExecution:
    if context.config.strategy == "Sota":
        return _execute_sota_evolve(context)
    return _execute_legacy_evolve(context)


def _execute_sota_evolve(context: PhaseContext) -> PhaseExecution:
    package_path = context.artifacts_dir / "research_candidate_package.json"
    package_path.unlink(missing_ok=True)
    matrix_path = context.require_phase_output("matrix", "feature_matrix.parquet")
    rolling_dir = context.artifacts_dir / "rolling_hyperopt"
    plan_path = rolling_dir / "hyperopt_walk_forward_plan.json"
    expected_result_path = rolling_dir / "hyperopt_walk_forward_result.json"
    acceptance_path = rolling_dir / "candidate_acceptance.json"
    rolling_package_path = rolling_dir / "candidate_package.json"
    for stale_path in (plan_path, expected_result_path, acceptance_path, rolling_package_path):
        stale_path.unlink(missing_ok=True)
    request = HyperoptWalkForwardRequest(
        exchange="binance",
        pair=context.config.symbol,
        timeframe=context.config.timeframe,
        strategy="Sota",
        start=date.fromisoformat(context.config.start),
        end=date.fromisoformat(context.config.end),
        train_days=context.config.hyperopt_train_days,
        test_days=context.config.hyperopt_test_days,
        step_days=context.config.hyperopt_step_days,
        epochs=context.config.hyperopt_epochs,
        random_state=context.config.hyperopt_random_state,
        min_trades=context.config.hyperopt_min_train_trades,
        spaces=("buy",),
        hyperopt_loss="MultiMetricHyperOptLoss",
        job_workers=context.config.hyperopt_workers,
        feature_matrix=matrix_path.relative_to(context.root_dir).as_posix(),
        run_id_prefix=context.config.run_id,
        skip_download=True,
    )
    result_path = run_hyperopt_walk_forward_validation(
        output_dir=rolling_dir,
        request=request,
        root_dir=context.root_dir,
        max_splits=context.config.hyperopt_max_splits,
    )
    if result_path.resolve() != expected_result_path.resolve():
        raise PhaseGateError("rolling hyperopt returned an unexpected result path")
    required_outputs = (plan_path, result_path, acceptance_path, rolling_package_path)
    rolling_outputs = tuple(
        path
        for path in required_outputs
        if path.is_file()
    )
    missing_outputs = [path.name for path in required_outputs if not path.is_file()]
    if missing_outputs:
        raise PhaseGateError(
            f"missing top-level rolling artifacts: {', '.join(missing_outputs)}",
            details={"evolution_engine": "rolling_hyperopt_walk_forward"},
            outputs=rolling_outputs,
        )
    result = _read_json_object(result_path, "rolling hyperopt result")
    plan = _read_json_object(plan_path, "rolling hyperopt plan")
    try:
        validate_hyperopt_walk_forward_evidence(
            request=request,
            max_splits=context.config.hyperopt_max_splits,
            plan=plan,
            result=result,
        )
    except ValueError as exc:
        raise PhaseGateError(
            f"rolling hyperopt evidence does not match production config: {exc}",
            details={"evolution_engine": "rolling_hyperopt_walk_forward"},
            outputs=rolling_outputs,
        ) from exc
    acceptance = _read_json_object(acceptance_path, "rolling candidate acceptance")
    if result.get("candidate_acceptance") != acceptance:
        raise PhaseGateError(
            "rolling candidate acceptance does not match the rolling result",
            details={"candidate_acceptance": acceptance},
            outputs=rolling_outputs,
        )
    if acceptance.get("passed") is not True:
        raise PhaseGateError(
            f"rolling candidate acceptance failed: {_failed_reasons(acceptance, 'candidate rejected')}",
            details={
                "candidate_acceptance": acceptance,
                "evolution_engine": "rolling_hyperopt_walk_forward",
                "rolling_result": result_path.relative_to(context.root_dir).as_posix(),
            },
            outputs=rolling_outputs,
        )
    try:
        written_package_path = write_sota_candidate_package(
            plan_path=plan_path,
            result_path=result_path,
            rolling_package_path=rolling_package_path,
            candidate_acceptance_path=acceptance_path,
            runtime_matrix_path=matrix_path,
            output_path=package_path,
            root_dir=context.root_dir,
        )
        package = _read_json_object(written_package_path, "Sota production candidate package")
    except (OSError, PhaseGateError, ValueError) as exc:
        package_path.unlink(missing_ok=True)
        raise PhaseGateError(
            f"Sota production package validation failed: {exc}",
            details={
                "candidate_acceptance": acceptance,
                "evolution_engine": "rolling_hyperopt_walk_forward",
                "rolling_result": result_path.relative_to(context.root_dir).as_posix(),
            },
            outputs=rolling_outputs,
        ) from exc
    qualification = package.get("qualification")
    selection = package.get("selection")
    if (
        package.get("schema_version") != 3
        or package.get("strategy") != "Sota"
        or package.get("passed") is not True
        or not isinstance(qualification, dict)
        or qualification.get("passed") is not True
        or not isinstance(selection, dict)
    ):
        package_path.unlink(missing_ok=True)
        raise PhaseGateError(
            "Sota production package is not accepted schema-3 evidence",
            details={
                "candidate_acceptance": acceptance,
                "evolution_engine": "rolling_hyperopt_walk_forward",
            },
            outputs=rolling_outputs,
        )
    return PhaseExecution(
        outputs=(*rolling_outputs, written_package_path),
        details={
            "candidate_acceptance": acceptance,
            "candidate_id": package.get("candidate_id"),
            "candidate_package": written_package_path.relative_to(context.root_dir).as_posix(),
            "evolution_engine": "rolling_hyperopt_walk_forward",
            "qualification": qualification,
            "rolling_metrics": package.get("rolling_metrics"),
            "selection": selection,
        },
    )


def _execute_legacy_evolve(context: PhaseContext) -> PhaseExecution:
    package_path = context.artifacts_dir / "research_candidate_package.json"
    package_path.unlink(missing_ok=True)
    matrix_path = context.require_phase_output("matrix", "feature_matrix.parquet")
    evolution_result_path = run_evolution(
        root_dir=context.root_dir,
        feature_matrix=matrix_path.relative_to(context.root_dir),
        run_id=f"{context.config.run_id}-evolution",
        candidates=default_candidate_space(),
        split_plan=build_evolution_split_plan(
            date.fromisoformat(context.config.start),
            date.fromisoformat(context.config.end),
        ),
    )
    result = _read_json_object(evolution_result_path, "evolution result")
    selection = result.get("selection")
    selected = result.get("selected_candidate")
    if (
        result.get("schema_version") != 2
        or result.get("final_oos_evaluation_count") != 1
        or not isinstance(selection, dict)
        or not isinstance(selected, dict)
        or selection.get("selected_candidate_id") != selected.get("candidate_id")
    ):
        raise PhaseGateError("evolution result has no structurally valid frozen candidate")
    qualification = evaluate_research_qualification(selected)
    if qualification.get("passed") is not True:
        reasons = qualification.get("failed_reasons")
        reason_text = "; ".join(str(reason) for reason in reasons) if isinstance(reasons, list) else ""
        raise PhaseGateError(f"selected evolution candidate failed final OOS promotion: {reason_text}")
    package_path = write_research_candidate_package(
        candidate=selected,
        evolution_result_path=evolution_result_path,
        output_path=package_path,
        root_dir=context.root_dir,
    )
    ranking_path = evolution_result_path.with_name("evolution_ranking.json")
    outputs = [evolution_result_path, package_path]
    if ranking_path.is_file():
        outputs.append(ranking_path)
    return PhaseExecution(
        outputs=tuple(outputs),
        details={
            "candidate_id": selected.get("candidate_id"),
            "candidate_package": package_path.relative_to(context.root_dir).as_posix(),
            "qualification": qualification,
            "split_plan": result.get("split_plan"),
        },
    )


def _execute_dry_run(context: PhaseContext) -> PhaseExecution:
    matrix_path = context.require_phase_output("matrix", "feature_matrix.parquet")
    package_path = context.require_phase_output("evolve", "research_candidate_package.json")
    result = launch_dry_run(
        root_dir=context.root_dir,
        matrix_path=matrix_path.relative_to(context.root_dir),
        candidate_package_path=package_path,
        manifest_path=context.store.manifest_path,
        env=context.env,
        runner=context.runner,
    )
    if result.returncode != 0:
        raise PhaseGateError(f"dry-run launch command failed with exit code {result.returncode}")
    package = json.loads(package_path.read_text())
    outputs: tuple[Path, ...] = ()
    if isinstance(package, dict) and package.get("schema_version") == 3:
        receipt_path = dry_run_receipt_path(
            root_dir=context.root_dir,
            candidate_package_path=package_path,
        )
        if not receipt_path.is_file():
            raise PhaseGateError("successful Sota dry-run launch did not produce a runtime receipt")
        outputs = (receipt_path,)
    return PhaseExecution(outputs=outputs, details=_command_details(result, context.env))


def _execute_live(context: PhaseContext) -> PhaseExecution:
    matrix_path = context.require_phase_output("matrix", "feature_matrix.parquet")
    package_path = context.require_phase_output("evolve", "research_candidate_package.json")
    readiness_path = context.artifacts_dir / "live_readiness.json"
    result = launch_live(
        root_dir=context.root_dir,
        matrix_path=matrix_path.relative_to(context.root_dir),
        candidate_package_path=package_path,
        readiness_path=readiness_path,
        manifest_path=context.store.manifest_path,
        confirm_live=context.config.confirm_live,
        env=context.env,
        runner=context.runner,
        run_dir=context.run_dir,
    )
    if result.returncode != 0:
        raise PhaseGateError(f"live launch command failed with exit code {result.returncode}")
    return PhaseExecution(outputs=(readiness_path,), details=_command_details(result, context.env))


def _ingest_live_readiness(context: PhaseContext) -> None:
    package_path = context.require_phase_output("evolve", "research_candidate_package.json")
    readiness_path = context.artifacts_dir / "live_readiness.json"
    readiness_artifacts = validate_live_readiness_evidence(
        root_dir=context.root_dir,
        run_dir=context.run_dir,
        candidate_package_path=package_path,
        readiness_path=readiness_path,
    )
    context.store.attach_phase_outputs(
        "dry-run",
        [readiness_path, *readiness_artifacts],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the fail-closed BTC production cycle.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--through", choices=PHASES, default="evolve")
    parser.add_argument("--source-policy", choices=("auto", "free-only", "paid-required"), default="auto")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--strategy", choices=tuple(PRODUCTION_STRATEGY_TIMEFRAMES), default="Sota")
    parser.add_argument("--hyperopt-epochs", type=int, default=100)
    parser.add_argument("--hyperopt-random-state", type=int, default=42)
    parser.add_argument("--hyperopt-train-days", type=int, default=365)
    parser.add_argument("--hyperopt-test-days", type=int, default=90)
    parser.add_argument("--hyperopt-step-days", type=int, default=90)
    parser.add_argument("--hyperopt-min-train-trades", type=int, default=5)
    parser.add_argument("--hyperopt-workers", type=int, default=1)
    parser.add_argument("--hyperopt-max-splits", type=int)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    return parser


def parse_config(args: argparse.Namespace, *, parser: argparse.ArgumentParser) -> ProductionRunConfig:
    if args.confirm_live and args.through != "live":
        parser.error("--confirm-live is valid only with --through live")
    if args.through == "live" and not args.confirm_live:
        parser.error("--through live requires --confirm-live")
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError:
        parser.error("--start and --end must use YYYY-MM-DD")
    if start >= end:
        parser.error("--start must be before --end")
    root_dir = args.root_dir.resolve()
    config = ProductionRunConfig(
        root_dir=root_dir,
        run_id=args.run_id,
        through=args.through,
        source_policy=args.source_policy,
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=start.isoformat(),
        end=end.isoformat(),
        git_revision=_git_revision(root_dir),
        strategy=args.strategy,
        confirm_live=args.confirm_live,
        hyperopt_epochs=args.hyperopt_epochs,
        hyperopt_random_state=args.hyperopt_random_state,
        hyperopt_train_days=args.hyperopt_train_days,
        hyperopt_test_days=args.hyperopt_test_days,
        hyperopt_step_days=args.hyperopt_step_days,
        hyperopt_min_train_trades=args.hyperopt_min_train_trades,
        hyperopt_workers=args.hyperopt_workers,
        hyperopt_max_splits=args.hyperopt_max_splits,
    )
    try:
        _validate_config(config)
    except ValueError as exc:
        parser.error(str(exc))
    return config


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = parse_config(args, parser=parser)
    manifest_path = (
        config.root_dir / "user_data" / "production_runs" / config.run_id / "manifest.json"
    )
    if manifest_path.exists() and not args.resume:
        parser.error(f"production run already exists: {config.run_id}; use --resume")
    result_path = run_production_cycle(config, dict(os.environ), SubprocessCommandRunner())
    print(result_path)
    manifest = json.loads(result_path.read_text())
    phases = manifest.get("phases")
    target = phases.get(config.through) if isinstance(phases, dict) else None
    return 0 if isinstance(target, dict) and target.get("status") == "passed" else 1


def _candidate_reports(candidate: Mapping[str, object]) -> dict[str, dict[str, object]]:
    promotion = candidate.get("promotion")
    reports = promotion.get("reports") if isinstance(promotion, dict) else candidate.get("reports")
    if not isinstance(reports, dict):
        return {}
    return {
        str(name): report
        for name, report in reports.items()
        if isinstance(name, str) and isinstance(report, dict)
    }


def _successful_parquet_paths(
    root_dir: Path,
    summary: Mapping[str, object],
    timeframe: str,
) -> tuple[Path, ...]:
    datasets = summary.get("datasets")
    paths: set[Path] = set()
    if not isinstance(datasets, list):
        return ()
    for item in datasets:
        if not isinstance(item, dict) or item.get("status") != "enabled":
            continue
        artifacts = item.get("artifacts")
        parquet = artifacts.get("parquet") if isinstance(artifacts, dict) else None
        if not isinstance(parquet, dict):
            raise PhaseGateError("successful external dataset is missing its parquet artifact")
        paths.add(
            _verified_sync_artifact(
                root_dir,
                parquet,
                f"{item.get('provider')}/{item.get('dataset')} parquet",
            )
        )
    return tuple(sorted(paths))


def _sync_artifact_paths(
    root_dir: Path,
    summary: Mapping[str, object],
) -> tuple[Path, ...]:
    datasets = summary.get("datasets")
    if not isinstance(datasets, list):
        raise PhaseGateError("external data sync summary has no datasets")
    paths: set[Path] = set()
    for item in datasets:
        if not isinstance(item, dict):
            raise PhaseGateError("external data sync summary has a malformed dataset")
        artifacts = item.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != {
            "parquet",
            "provider_manifest",
            "raw",
        }:
            raise PhaseGateError("external dataset artifact set is incomplete")
        label = f"{item.get('provider')}/{item.get('dataset')}"
        for name in ("raw", "parquet", "provider_manifest"):
            artifact = artifacts[name]
            if not isinstance(artifact, dict):
                raise PhaseGateError(f"{label} {name} artifact is malformed")
            paths.add(_verified_sync_artifact(root_dir, artifact, f"{label} {name}"))
    return tuple(sorted(paths))


def _verified_sync_artifact(
    root_dir: Path,
    artifact: Mapping[str, object],
    label: str,
) -> Path:
    path_text = artifact.get("path")
    expected_hash = artifact.get("sha256")
    expected_size = artifact.get("size_bytes")
    if (
        not isinstance(path_text, str)
        or not path_text
        or not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
    ):
        raise PhaseGateError(f"{label} artifact evidence is incomplete")
    path = (root_dir / path_text).resolve()
    try:
        path.relative_to(root_dir.resolve())
    except ValueError as exc:
        raise PhaseGateError(f"{label} artifact is outside the root directory") from exc
    if (
        not path.is_file()
        or path.stat().st_size != expected_size
        or _sha256(path) != expected_hash
    ):
        raise PhaseGateError(f"{label} artifact does not match its sync evidence")
    return path


def _write_phase_report(
    context: PhaseContext,
    phase: str,
    *,
    status: str,
    details: Mapping[str, object],
    outputs: Sequence[Path],
) -> Path:
    report_path = context.run_dir / "phases" / f"{phase}.json"
    payload = {
        "details": details,
        "outputs": [_artifact_record(context.root_dir, path) for path in outputs],
        "phase": phase,
        "status": status,
    }
    _write_json_atomic(report_path, payload)
    return report_path


def _command_details(result: CommandResult, env: Mapping[str, str]) -> dict[str, object]:
    return {
        "command": list(result.command),
        "returncode": result.returncode,
        "stdout": _redact(result.stdout, env),
    }


def _artifact_record(root_dir: Path, path: Path) -> dict[str, object]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"artifact path is outside root directory: {path}") from exc
    if not resolved.is_file():
        return {"exists": False, "path": relative.as_posix(), "sha256": None, "size_bytes": None}
    return {
        "exists": True,
        "path": relative.as_posix(),
        "sha256": _sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _verified_candidate_config_artifact(
    candidate: Mapping[str, object],
    root_dir: Path,
) -> dict[str, object]:
    artifact = candidate.get("candidate_config_artifact")
    if not isinstance(artifact, dict):
        raise ValueError("candidate config artifact is missing")
    path_text = artifact.get("path")
    expected_hash = artifact.get("sha256")
    expected_size = artifact.get("size_bytes")
    if (
        artifact.get("exists") is not True
        or not isinstance(path_text, str)
        or not isinstance(expected_hash, str)
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
    ):
        raise ValueError("candidate config artifact is incomplete")
    candidate_path = Path(path_text)
    if not candidate_path.is_absolute():
        candidate_path = root_dir / candidate_path
    actual = _artifact_record(root_dir, candidate_path)
    if (
        actual.get("exists") is not True
        or actual.get("sha256") != expected_hash
        or actual.get("size_bytes") != expected_size
    ):
        raise ValueError("candidate config artifact does not match selected candidate")
    return actual


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary_path.replace(path)


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise PhaseGateError(f"invalid or missing {label}") from exc
    if not isinstance(payload, dict):
        raise PhaseGateError(f"{label} must be a JSON object")
    return payload


def _failed_reasons(payload: Mapping[str, object], fallback: str) -> str:
    reasons = payload.get("failed_reasons")
    if isinstance(reasons, list) and reasons:
        return "; ".join(str(reason) for reason in reasons)
    return fallback


def _timerange(config: ProductionRunConfig) -> str:
    return f"{config.start.replace('-', '')}-{config.end.replace('-', '')}"


def _provider_symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "")


def _validate_config(config: ProductionRunConfig) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", config.run_id) or config.run_id in (".", ".."):
        raise ValueError("production run_id must be a path-safe identifier")
    if config.through not in PHASES:
        raise ValueError(f"unknown production phase: {config.through}")
    if config.source_policy not in ("auto", "free-only", "paid-required"):
        raise ValueError(f"unsupported source policy: {config.source_policy}")
    if config.confirm_live and config.through != "live":
        raise ValueError("confirm_live is valid only for the live phase")
    if config.through == "live" and not config.confirm_live:
        raise ValueError("live phase requires confirm_live")
    if not config.symbol.strip():
        raise ValueError("production cycle symbol is required")
    if not config.timeframe.strip():
        raise ValueError("production cycle timeframe is required")
    _validate_strategy_timeframe(config.strategy, config.timeframe)
    start = date.fromisoformat(config.start)
    end = date.fromisoformat(config.end)
    if start >= end:
        raise ValueError("production cycle start must be before end")
    for name, value in (
        ("hyperopt_epochs", config.hyperopt_epochs),
        ("hyperopt_random_state", config.hyperopt_random_state),
        ("hyperopt_train_days", config.hyperopt_train_days),
        ("hyperopt_test_days", config.hyperopt_test_days),
        ("hyperopt_step_days", config.hyperopt_step_days),
        ("hyperopt_min_train_trades", config.hyperopt_min_train_trades),
        ("hyperopt_workers", config.hyperopt_workers),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if config.hyperopt_max_splits is not None and (
        not isinstance(config.hyperopt_max_splits, int)
        or isinstance(config.hyperopt_max_splits, bool)
        or config.hyperopt_max_splits <= 0
    ):
        raise ValueError("hyperopt_max_splits must be a positive integer")
    if config.strategy == "Sota":
        if (
            config.hyperopt_max_splits is not None
            and config.hyperopt_max_splits < SOTA_MIN_ROLLING_SPLITS
        ):
            raise ValueError(
                f"Sota production hyperopt_max_splits must be at least {SOTA_MIN_ROLLING_SPLITS}"
            )
        if len(config.rolling_hyperopt_splits()) < SOTA_MIN_ROLLING_SPLITS:
            raise ValueError(
                f"Sota production date geometry must yield at least "
                f"{SOTA_MIN_ROLLING_SPLITS} OOS splits"
            )
        return
    build_evolution_split_plan(start, end)


def _validate_strategy_timeframe(strategy: str, timeframe: str) -> None:
    supported_timeframes = PRODUCTION_STRATEGY_TIMEFRAMES.get(strategy)
    if supported_timeframes is None:
        raise ValueError(f"unsupported production strategy: {strategy}")
    if timeframe not in supported_timeframes:
        expected = ", ".join(sorted(supported_timeframes))
        raise ValueError(f"{strategy} requires production timeframe: {expected}")


def _git_revision(root_dir: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=True,
    )
    head = completed.stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
        cwd=root_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
    ).stdout
    if not diff:
        return head
    return f"{head}+dirty:{hashlib.sha256(diff).hexdigest()}"


def _redact(value: str, env: Mapping[str, str]) -> str:
    redacted = value
    secrets = sorted({item for item in env.values() if item}, key=len, reverse=True)
    for secret in secrets:
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_PHASE_EXECUTORS: dict[str, PhaseExecutor] = {
    "collect": _execute_collect,
    "matrix": _execute_matrix,
    "research": _execute_research,
    "evolve": _execute_evolve,
    "dry-run": _execute_dry_run,
    "live": _execute_live,
}


if __name__ == "__main__":
    raise SystemExit(main())

