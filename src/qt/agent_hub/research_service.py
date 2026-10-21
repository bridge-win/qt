"""Internal no-egress worker reusing QT's real Lab and Nautilus research engine.

Run inside Dockerfile.workbench's native runtime with a private state volume:
python -m uvicorn qt.agent_hub.research_service:create_research_app --factory
  --host 0.0.0.0 --port 8092 --workers 1
"""

from __future__ import annotations

import copy
import hmac
import importlib.metadata
import json
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException, Request

from qt.agent_hub.quant_validation import (
    annualized_return,
    audit_native_timeline,
    fold_bootstrap,
)
from qt.agent_hub.research import (
    ValidationReport,
    ValidationRequest,
    digest,
    native_bridge_fingerprint,
)


class NativeResearch:
    """One bounded native experiment at a time, cached by all executable inputs."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def run(self, request: ValidationRequest) -> dict[str, Any]:
        import pandas as pd

        from qt.lab.persistence import LabRepository
        from qt.lab.schemas import StrategyContent
        from qt.lab.service import LabService, ParquetTimelineProvider, build_validation_folds
        from qt.nautilus.executor import NautilusResearchExecutor
        from qt.nautilus.runner import NativeLedgerError
        from qt.research.analysis import block_bootstrap_summary, deflated_sharpe_details
        from qt.research.datasets import DatasetCatalog

        # Parse the exact existing Lab grammar before importing native executors.
        content = request.spec.strategy_version.content
        StrategyContent.model_validate(copy.deepcopy(content))
        engine = engine_identity()
        run_hash = digest({"input_hash": request.input_hash(), "engine": engine})
        directory = self.root / request.tenant_id / run_hash
        directory.mkdir(parents=True, exist_ok=True)
        report_path = directory / "report.json"
        if report_path.exists():
            cached = ValidationReport.model_validate_json(report_path.read_bytes())
            if cached.input_hash != request.input_hash() or cached.engine != engine:
                raise ValueError("cached research identity mismatch")
            return cached.model_dump()
        parquet_root = directory / "parquet"
        data_path = parquet_root / "ohlcv" / "binance_BTCUSDT_1d.parquet"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame([bar.model_dump() for bar in request.bars])
        frame.index = pd.to_datetime(frame.pop("timestamp"), unit="ms", utc=True)
        frame.index.name = "timestamp"
        frame.to_parquet(data_path)
        _write_json(
            data_path.with_suffix(".manifest.json"),
            {
                "provider": "binance",
                "symbol": "BTC/USDT",
                "timeframe": "1d",
                "market": "spot",
                "source": request.spec.data.source,
                "input_hash": request.input_hash(),
                "bars_sha256": request.spec.data.bars_sha256,
            },
        )
        dataset = DatasetCatalog(parquet_root).get("binance-btcusdt-1d")
        spec = request.spec
        experiment = {
            "dataset_id": dataset["dataset_id"],
            "dataset_fingerprint": dataset["fingerprint"],
            "assumptions": spec.costs.model_dump(),
            "benchmark": "buy_and_hold_same_oos_window",
            "cashflow": {"mode": "none"},
            "market": "spot",
        }
        native = _AttestedExecutor(
            NautilusResearchExecutor(parquet_root, directory / "artifacts"),
            snapshot_start_ms=request.bars[0].timestamp,
        )
        repository = LabRepository(directory / "research.sqlite3")
        lab = LabService(
            repository, native, timeline_provider=ParquetTimelineProvider(parquet_root)
        )
        payload = {
            "strategy_version_id": spec.strategy_version.version_id,
            "strategy_version": {
                "version_id": spec.strategy_version.version_id,
                "content": content,
            },
            "profile": "quick",
            "seed": spec.seed,
            "experiment": experiment,
            "candidate_parameters": [{}],
            "cscv_diagnostic": False,
            **spec.walk_forward.model_dump(),
        }
        # The existing standard selector requires a finite selection objective.
        # This bridge has one fixed version and no search: flat validation
        # windows are legitimate evidence, not an invented zero Sharpe score.
        timeline = ParquetTimelineProvider(parquet_root).timestamps(str(dataset["dataset_id"]))
        windows = build_validation_folds(
            timeline[-spec.walk_forward.required_bars() :],
            **spec.walk_forward.model_dump(),
        )
        folds: list[dict[str, Any]] = []
        test_results: list[dict[str, Any]] = []
        for window in windows:
            phases: dict[str, dict[str, Any]] = {}
            for phase in ("train", "validation", "test"):
                phase_payload = {
                    **payload,
                    "experiment": {
                        **experiment,
                        "from": getattr(window, phase + "_start"),
                        "to": getattr(window, phase + "_end"),
                        "lab_phase": phase,
                        "lab_fold": window.number,
                    },
                }
                result = cast(
                    dict[str, Any],
                    lab.execute(
                        {"job_type": "lab_validation", "payload": phase_payload},
                        lambda *_: None,
                        lambda: False,
                    )["result"],
                )
                phases[phase] = result
            test_results.append(phases["test"])
            folds.append(
                {
                    "fold": window.__dict__,
                    "selected_parameters": {},
                    "train_metrics": phases["train"]["metrics"],
                    "validation_metrics": phases["validation"]["metrics"],
                    "test_metrics": phases["test"]["metrics"],
                    "test_result_id": phases["test"]["run_id"],
                    "temporal_audit": {
                        phase: phases[phase]["hub_temporal_audit"]
                        for phase in ("train", "validation", "test")
                    },
                }
            )
        # Metamorphic check: removing all data after the first OOS window must
        # leave its decisions and native PnL exactly unchanged. This is bounded
        # evidence for this engine/version, not a blanket independent audit.
        audit_root = directory / "prefix_audit" / "parquet"
        audit_path = audit_root / "ohlcv" / "binance_BTCUSDT_1d.parquet"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        prefix_frame = frame.loc[: windows[0].test_end]
        prefix_frame.to_parquet(audit_path)
        _write_json(
            audit_path.with_suffix(".manifest.json"),
            {
                "provider": "binance",
                "symbol": "BTC/USDT",
                "timeframe": "1d",
                "market": "spot",
                "source": spec.data.source,
                "parent_input_hash": request.input_hash(),
                "bars_sha256": digest(
                    [bar.model_dump() for bar in request.bars[: len(prefix_frame)]]
                ),
                "transformation": "truncate_after_first_oos_window",
            },
        )
        audit_dataset = DatasetCatalog(audit_root).get("binance-btcusdt-1d")
        auditor = _AttestedExecutor(
            NautilusResearchExecutor(audit_root, directory / "prefix_audit" / "artifacts"),
            snapshot_start_ms=request.bars[0].timestamp,
        )
        prefix_result = auditor.execute(
            {
                **experiment,
                "dataset_fingerprint": audit_dataset["fingerprint"],
                "from": windows[0].test_start,
                "to": windows[0].test_end,
                "seed": spec.seed,
                "mode": "lab_strategy_version",
                "lab_strategy_version": payload["strategy_version"],
                "parameter_overrides": {},
                "lab_phase": "test",
                "lab_fold": windows[0].number,
            },
            lambda *_: None,
            lambda: False,
        )
        prefix_fields = ("series", "decision_traces", "account_invariants")
        full_hash = digest({key: test_results[0][key] for key in prefix_fields})
        prefix_hash = digest({key: prefix_result[key] for key in prefix_fields})
        if full_hash != prefix_hash:
            raise ValueError("native prefix invariance failed: future data changes OOS evidence")
        oos = pd.Series(
            [
                float(value)
                for result in test_results
                for _, value in sorted(result["series"]["returns"].items())
            ]
        )
        statistics: dict[str, Any] = {
            "deflated_sharpe": deflated_sharpe_details(
                oos,
                attempted_variants=1,
                trial_sharpe_variance=None,
                trial_scope={
                    "candidate_count": 1,
                    "selection_events": 0,
                    "scope": "fixed strategy; no parameter search",
                },
            ),
            "pbo": {
                "status": "not_applicable",
                "reason": "no parameter search or selection matrix",
            },
            "search_accounting": {
                "submitted_candidates": 1,
                "selection_events_in_this_run": 0,
                "cross_run_search_history": "not_registered",
                "global_selection_bias_corrected": False,
                "statistic_interpretation": (
                    "single-candidate DSR reduces to PSR against zero; "
                    "not evidence that no other strategies or prompts were tried"
                ),
            },
            "block_bootstrap": block_bootstrap_summary(
                oos, simulations=200, block_size=7, seed=spec.seed
            ),
            "fold_bootstrap": fold_bootstrap(
                [list(result["series"]["returns"].values()) for result in test_results],
                seed=spec.seed,
            ),
        }
        statistics["block_bootstrap"]["scope"] = (
            "legacy pooled diagnostic; may cross reset boundaries; not used for gate"
        )
        statistics["block_bootstrap"].pop("percentile_paths", None)
        stressed_results, execution_stress_results, baselines = [], [], []
        execution_stress_failures = []
        for fold in folds:
            window = fold["fold"]
            common = {
                **experiment,
                "from": window["test_start"],
                "to": window["test_end"],
                "seed": spec.seed,
                "mode": "lab_strategy_version",
                "lab_phase": "test",
                "lab_fold": window["number"],
                "parameter_overrides": {},
            }
            stress = {
                **common,
                "lab_strategy_version": payload["strategy_version"],
                "assumptions": {**spec.costs.model_dump(), "fee_bps": spec.costs.fee_bps * 2},
            }
            baseline = {
                **common,
                "lab_strategy_version": {
                    "version_id": "qt-buy-and-hold-1.0.0",
                    "content": {
                        "mode": "builtin",
                        "builtin_identity": "qt:buy_and_hold",
                        "builtin_version": "1.0.0",
                    },
                },
            }
            stressed_results.append(native.execute(stress, lambda *_: None, lambda: False))
            try:
                delayed = native.execute(
                    {
                        **stress,
                        "assumptions": {
                            **stress["assumptions"],
                            "latency_nanos": 86_400_000_000_000,
                            "one_tick_slippage": True,
                            "slippage_probability": 1.0,
                        },
                    },
                    lambda *_: None,
                    lambda: False,
                )
                execution_stress_results.append(delayed)
            except NativeLedgerError:
                # Price gaps can invalidate a quantity sized before delayed
                # arrival. Preserve the failed experiment; never replace native
                # accounting with invented fills or promote a partial stress run.
                execution_stress_failures.append(
                    {"fold": window["number"], "reason": "native_account_invariants_failed"}
                )
            baselines.append(native.execute(baseline, lambda *_: None, lambda: False))
        metrics = _combine_metrics(test_results)
        stress_metrics = _combine_metrics(stressed_results)
        execution_stress_metrics = (
            None if execution_stress_failures else _combine_metrics(execution_stress_results)
        )
        baseline_metrics = _combine_metrics(baselines)
        failed = []
        if metrics["sharpe"] is None or metrics["sharpe"] < 0.5:
            failed.append("oos_sharpe_below_0.5_or_undefined")
        if metrics["max_drawdown"] < -0.35:
            failed.append("oos_drawdown_exceeds_35_percent")
        if stress_metrics["total_return"] <= 0:
            failed.append("nonpositive_oos_return_under_2x_fees")
        if execution_stress_metrics is None:
            failed.append("delayed_execution_accounting_unverified")
        elif execution_stress_metrics["total_return"] <= 0:
            failed.append("nonpositive_oos_return_under_delayed_execution_stress")
        if statistics["fold_bootstrap"]["worst_return_p05"] <= 0:
            failed.append("oos_block_bootstrap_lower_bound_not_positive")
        if any(fold["test_metrics"]["max_drawdown"] < -0.35 for fold in folds):
            failed.append("individual_oos_fold_drawdown_exceeds_35_percent")
        if not (
            metrics["total_return"] > baseline_metrics["total_return"]
            or (
                metrics["total_return"] > 0
                and baseline_metrics["max_drawdown"] < 0
                and abs(metrics["max_drawdown"]) <= abs(baseline_metrics["max_drawdown"]) * 0.8
            )
        ):
            failed.append("neither_beats_baseline_nor_reduces_drawdown_20_percent")
        report = ValidationReport(
            eligible_for_paper=not failed,
            spec=spec,
            execution_signal=execution_signal(request),
            input_hash=request.input_hash(),
            strategy_version_id=spec.strategy_version.version_id,
            strategy_sha256=spec.strategy_version.content_sha256,
            data_sha256=spec.data.bars_sha256,
            engine=engine,
            validation_status="strict_walk_forward",
            integrity={
                "account_invariants": "verified",
                "account_invariants_scope": (
                    "completed train/validation/OOS/fee-stress/baseline/prefix runs; "
                    "delayed execution has a separate required status"
                ),
                "execution_stress_verified": not execution_stress_failures,
                "native_runs": native.runs + auditor.runs,
                "native_attempts": native.attempts + auditor.attempts,
                "snapshot_rows": len(request.bars),
                "snapshot_parquet_sha256": dataset["fingerprint"],
                "gap_free": True,
                "closed_bars_only": True,
                "selection_policy": "fixed submitted version; no tuning on train, validation or test",
                "parameter_search": "none; exact submitted immutable version",
                "lookahead_audit": "bounded_native_prefix_and_observation_audit",
                "temporal_audit": "every_verified_native_run_has_exact_closed_bar_coverage",
                "prefix_invariance": {
                    "status": "verified",
                    "fold": windows[0].number,
                    "removed_future_bars": len(frame) - len(prefix_frame),
                    "full_evidence_sha256": full_hash,
                    "prefix_evidence_sha256": prefix_hash,
                    "compared": list(prefix_fields),
                    "scope": "first OOS fold; not a provider-revision or independent code audit",
                },
                "purge_embargo_scope": (
                    "chronological gaps for a fixed non-fitted strategy; "
                    "not event-label purging or CPCV; no learned-label horizon asserted"
                ),
                "signal_initialization": "same cash-reset strategy state as final OOS fold",
            },
            metrics=metrics,
            folds=folds,
            statistics=statistics,
            costs={
                "base": spec.costs.model_dump(),
                "2x_fees_oos": stress_metrics,
                "execution_stress_oos": {
                    "status": "failed_native_account_invariants"
                    if execution_stress_failures else "verified",
                    "failed_folds": execution_stress_failures,
                    "assumptions": {
                        "fee_bps": spec.costs.fee_bps * 2,
                        "latency_nanos": 86_400_000_000_000,
                        "slippage": "one_price_tick",
                        "slippage_probability": 1.0,
                    },
                    "metrics": execution_stress_metrics,
                    "interpretation": (
                        "native delayed arrival against later bar close; "
                        "not next-open fills or a spread/market-impact model"
                    ),
                },
                "base_execution_timing": (
                    "zero-latency native orders settle against completed-bar close; "
                    "optimistic for real-time close-triggered execution"
                ),
            },
            benchmarks={"buy_and_hold_same_oos": baseline_metrics},
            verdict="fragile" if failed else "paper_research_candidate",
            failed_gates=failed,
            limitations=[
                "Validates only the exact strategy, instrument, data snapshot and costs in input_hash",
                "OHLCV bar model does not represent bps slippage, orderbook impact or queue position",
                "OOS folds reset portfolio capital; aggregate returns describe the fold protocol",
                "Not a historical provider revision or independent lookahead audit",
                "No complete cross-run search ledger or unseen final holdout; single-candidate PSR is not global DSR",
                "Bootstrap bounds condition on observed folds and block sizes, not future-profit probabilities",
                "Fixed chronological gaps are not event-label-aware purging or CPCV",
                "Research candidate requires forward paper evidence; never live-trading approval",
                "Does not validate Polymarket probabilities or unrelated model-generated decisions",
            ],
        ).model_dump()
        _write_json(directory / "request.json", request.model_dump())
        _write_json(report_path, report)
        return report


class _AttestedExecutor:
    def __init__(self, executor: Any, *, snapshot_start_ms: int | None = None) -> None:
        self.executor, self.runs = executor, 0
        self.attempts = 0
        self.snapshot_start_ms = snapshot_start_ms

    def execute(
        self,
        spec: Mapping[str, object],
        progress: Callable[..., None],
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        self.attempts += 1
        result = cast(dict[str, Any], self.executor.execute(spec, progress, cancelled))
        if result.get("engine") != "nautilus_trader":
            raise ValueError("unexpected research engine")
        account = result.get("account_invariants", {})
        if account.get("status") != "verified" or account.get("violations") != []:
            raise ValueError("native account invariants failed")
        if result.get("data", {}).get("fingerprint") != spec["dataset_fingerprint"]:
            raise ValueError("native dataset identity changed")
        if result.get("comparability", {}).get("window") != {
            "from": spec.get("from"),
            "to": spec.get("to"),
        }:
            raise ValueError("native executor ignored chronological window")
        if self.snapshot_start_ms is not None:
            audit = audit_native_timeline(
                result,
                start=str(spec["from"]),
                end=str(spec["to"]),
                snapshot_start_ms=self.snapshot_start_ms,
            )
            result["hub_temporal_audit"] = audit
            # Native source equity begins before warmup. Its elapsed-time CAGR
            # therefore dilutes the decision-window return; correct the bridge
            # display using the audited daily decision observations.
            result["metrics"]["cagr"] = annualized_return(
                result["metrics"]["total_return"], audit["bars"]
            )
            cagr, drawdown = result["metrics"]["cagr"], result["metrics"]["max_drawdown"]
            result["metrics"]["calmar"] = (
                cagr / abs(drawdown) if cagr is not None and drawdown < 0 else None
            )
            result["metrics"]["annualization_window"] = "audited_decision_bars_only"
        self.runs += 1
        return result


def _combine_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    import math
    import statistics

    values: list[float] = []
    for result in results:
        returns = result.get("series", {}).get("returns")
        if not isinstance(returns, dict) or not returns:
            raise ValueError("native result has no marked-to-market returns")
        rows = [float(value) for _, value in sorted(returns.items())]
        if any(not math.isfinite(value) or value < -1 for value in rows):
            raise ValueError("native return series is invalid")
        values.extend(rows)
    equity, peak, drawdown = 1.0, 1.0, 0.0
    for value in values:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    volatility = statistics.stdev(values) if len(values) > 1 else 0
    return {
        "total_return": equity - 1,
        "cagr": annualized_return(equity - 1, len(values)),
        "max_drawdown": drawdown,
        "sharpe": statistics.mean(values) / volatility * math.sqrt(365.25) if volatility else None,
        "bars": len(values),
        "folds": len(results),
        "returns_source": "native_portfolio_mark_to_market",
    }


def execution_signal(request: ValidationRequest) -> dict[str, Any]:
    """Replay the exact built-in strategy, not a second look-alike SMA formula.

    BUY/SELL here expresses a long-only target, not a quantity or order. The
    execution owner must compare existing inventory and avoid repeated entries.
    Rules and other built-ins remain research-only until their signal adapters
    receive equivalent tests against their exact native execution semantics.
    """
    spec = request.spec
    signal = {
        "strategy_version_id": spec.strategy_version.version_id,
        "strategy_sha256": spec.strategy_version.content_sha256,
        "data_sha256": spec.data.bars_sha256,
        "observed_at": spec.data.observed_at,
        "as_of_bar": request.bars[-1].timestamp,
        "side": "HOLD",
        "target_weight": None,
        "method": "unsupported_strategy_signal_adapter",
    }
    content = spec.strategy_version.content
    if content.get("mode") != "builtin" or content.get("builtin_identity") != "qt:sma_crossover":
        return signal
    from decimal import Decimal

    import pandas as pd
    from btc_backtest.data.models import DataRequest
    from btc_backtest.engine.models import BacktestSpec, PortfolioSnapshot
    from btc_backtest.strategies.base import InitializationContext, StrategyContext
    from btc_backtest.strategies.registry import default_strategy_registry

    strategy = default_strategy_registry().create(
        "sma_crossover", content.get("builtin_parameters", {})
    )
    if content.get("builtin_version") != strategy.metadata.version:
        raise ValueError("execution signal strategy version mismatch")
    frame = pd.DataFrame([bar.model_dump() for bar in request.bars])
    frame.index = pd.to_datetime(frame.pop("timestamp"), unit="ms", utc=True)
    data = DataRequest(
        provider="binance",
        symbol="BTC/USDT",
        timeframe="1d",
        market="spot",
        start=frame.index[0].to_pydatetime(),
        end=(frame.index[-1] + pd.Timedelta(days=1)).to_pydatetime(),
    )
    backtest = BacktestSpec(
        strategy="sma_crossover",
        strategy_params=content.get("builtin_parameters", {}),
        data=data,
        initial_cash=Decimal(str(spec.costs.initial_cash)),
        fee_bps=Decimal(str(spec.costs.fee_bps)),
        slippage_bps=Decimal(0),
        seed=spec.seed,
    )
    strategy.initialize(
        InitializationContext(spec=backtest, data_manifests=(), parameters=backtest.strategy_params)
    )
    target = Decimal(0)
    # Native folds start from cash and reset state. Warmup bars are observable
    # but do not call target_weight before decision_start. Replaying the entire
    # history here would retain an earlier crossing the final OOS fold never saw.
    decision_start = len(frame) - spec.walk_forward.test_bars
    for count in range(decision_start + 1, len(frame) + 1):
        timestamp = (frame.index[count - 1] + pd.Timedelta(days=1)).to_pydatetime()
        portfolio = PortfolioSnapshot(
            timestamp=timestamp,
            cash=backtest.initial_cash,
            equity=backtest.initial_cash,
            realized_pnl=Decimal(0),
            unrealized_pnl=Decimal(0),
            positions=(),
        )
        context = StrategyContext(
            timestamp=timestamp,
            bars=frame.iloc[:count],
            portfolio=portfolio,
            parameters=backtest.strategy_params,
        )
        target = strategy.target_weight(context)
    signal.update(
        {
            "side": "BUY" if target > 0 else "SELL",
            "target_weight": str(target),
            "method": "exact_builtin_target_weight_replay",
            "initialization_scope": "final_oos_fold_cash_reset",
            "decision_start_bar": request.bars[decision_start].timestamp,
        }
    )
    return signal


def engine_identity() -> dict[str, Any]:
    import btc_backtest.strategies

    from qt.lab import service as lab
    from qt.nautilus import executor
    from qt.research import analysis
    from qt.workbench import strategy_factory

    source_hashes = {}
    for module in (lab, analysis, strategy_factory):
        if module.__file__ is None:
            raise RuntimeError("native source module has no file identity")
        source_hashes[module.__name__] = digest(Path(module.__file__).read_text())
    for prefix, directory in (
        ("qt.nautilus", Path(executor.__file__).parent),
        ("btc_backtest.strategies", Path(btc_backtest.strategies.__file__).parent),
    ):
        for path in sorted(directory.rglob("*.py")):
            source_hashes[prefix + "/" + path.relative_to(directory).as_posix()] = digest(
                path.read_text()
            )
    source_hashes[__name__] = digest(Path(__file__).read_text())
    return {
        "name": "nautilus_trader",
        "version": importlib.metadata.version("nautilus_trader"),
        "lab": "qt.lab.service.LabService",
        "source_sha256": digest(source_hashes),
        "native_bridge_sha256": native_bridge_fingerprint(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("btc-backtest", "numpy", "pandas", "pyarrow")
        },
    }


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def create_research_app(root: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="QT native research bridge", docs_url=None, redoc_url=None, openapi_url=None
    )
    executor = NativeResearch(
        root or Path(os.environ.get("QT_AGENT_HUB_RESEARCH_STATE", "/state/research"))
    )
    semaphore = threading.BoundedSemaphore(1)

    @app.middleware("http")
    async def bounded_body(request: Request, call_next: Any) -> Any:
        from starlette.responses import JSONResponse

        if request.method == "POST":
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 1_000_000:
                    return JSONResponse(
                        {"detail": "research request exceeds bound"}, status_code=413
                    )
            request._body = bytes(data)
        return await call_next(request)

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"ok": True, "engine": engine_identity(), "arbitrary_code": False}

    @app.post("/internal/validate")
    def validate(
        body: ValidationRequest, authorization: str = Header(default="")
    ) -> dict[str, Any]:
        token = os.environ.get("QT_AGENT_HUB_EXECUTOR_TOKEN", "")
        if not token or not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "research authentication required")
        if not semaphore.acquire(blocking=False):
            raise HTTPException(429, "research worker busy", headers={"Retry-After": "5"})
        try:
            return executor.run(body)
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, "invalid_or_unsupported_research_contract") from None
        finally:
            semaphore.release()

    return app
