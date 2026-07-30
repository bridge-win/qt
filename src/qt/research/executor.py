"""Engine-backed execution and immutable artifacts for research jobs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import TypeAlias

import numpy as np
import pandas as pd
from btc_backtest.data.models import MarketBundle
from btc_backtest.engine.models import BacktestResult, Fill
from btc_backtest.engine.runner import EventRunner
from btc_backtest.reporting.metrics import compute_metrics
from btc_backtest.strategies.base import Strategy
from btc_backtest.strategies.registry import default_strategy_registry
from btc_backtest.validation.monte_carlo import BlockBootstrap

from qt.backtest.strategy_backtest import (
    _equity_series,
    _market_dataset,
    _request_for,
    _trades_frame,
)
from qt.research.analysis import deflated_sharpe_probability
from qt.research.datasets import DatasetCatalog
from qt.research.service import build_strategy
from qt.research.validation import evaluate_research_verdict

JsonDict: TypeAlias = dict[str, object]
ProgressCallback = Callable[[str, int], None]
CancellationCheck = Callable[[], bool]


class ResearchExecutor:
    """Run one normalized research specification against the QT event engine."""

    def __init__(self, parquet_root: Path, artifact_root: Path) -> None:
        self.parquet_root = parquet_root
        self.artifact_root = artifact_root
        self.datasets = DatasetCatalog(parquet_root)

    def execute(
        self,
        spec: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict:
        progress("data_validation", 5)
        self._check_cancelled(cancelled)
        dataset_id = str(spec["dataset_id"])
        dataset = self.datasets.get(dataset_id)
        profile = str(spec.get("validation_profile", "standard"))
        if profile == "standard" and (
            dataset_id != "bitstamp-btcusd-1d-10y"
            or not bool(dataset.get("standard_ready"))
        ):
            raise ValueError(
                "standard validation requires the ready 10-year Bitstamp "
                "BTC/USD daily dataset"
            )
        frame = self._load_frame(dataset_id)
        self._validate_frame(frame)

        progress("simulation", 35)
        self._check_cancelled(cancelled)
        primary = self._run(spec, frame, build_strategy(spec))

        progress("benchmarks", 50)
        self._check_cancelled(cancelled)
        buy_hold = self._run(
            spec,
            frame,
            default_strategy_registry().create("buy_and_hold", {}),
        )
        fixed_dca = self._run(
            spec,
            frame,
            default_strategy_registry().create("fixed_dca", {}),
        )
        primary_metrics = _metrics(primary)
        buy_hold_metrics = _metrics(buy_hold)
        fixed_dca_metrics = _metrics(fixed_dca)

        progress("validation_pipeline", 52 if profile == "standard" else 75)
        self._check_cancelled(cancelled)
        evidence = (
            self._standard_evidence(
                spec,
                frame,
                progress,
                cancelled,
            )
            if profile == "standard"
            else self._quick_evidence(primary_metrics, buy_hold_metrics)
        )
        verdict = (
            evaluate_research_verdict(evidence)
            if profile == "standard"
            else {
                "status": "fragile",
                "label": (
                    "Quick diagnostic only: run the standard profile before "
                    "considering paper research."
                ),
                "failed_gates": [
                    "Standard robustness and integrity gates were not run."
                ],
                "live_ready": False,
            }
        )

        run_id = uuid.uuid4().hex
        result: JsonDict = {
            "schema_version": "2",
            "run_id": run_id,
            "strategy": str(spec["strategy_id"]),
            "mode": str(spec["mode"]),
            "validation_profile": profile,
            "configuration": _json_safe(dict(spec)),
            "data": {
                "dataset_id": dataset_id,
                "provider": dataset["provider"],
                "symbol": dataset["symbol"],
                "timeframe": dataset["timeframe"],
                "rows": len(frame),
                "start": frame.index[0].isoformat(),
                "end": frame.index[-1].isoformat(),
                "fingerprint": dataset["fingerprint"],
            },
            "metrics": primary_metrics,
            "benchmarks": {
                "buy_and_hold": {"metrics": buy_hold_metrics},
                "fixed_dca": {"metrics": fixed_dca_metrics},
            },
            "validation": evidence,
            "verdict": verdict,
            "artifacts": [
                "data_manifest.json",
                "equity.csv",
                "summary.json",
                "trades.csv",
            ],
        }
        progress("artifacts", 90)
        self._write_artifacts(result, primary, buy_hold, fixed_dca)
        progress("completed", 100)
        return result

    def _standard_evidence(
        self,
        spec: Mapping[str, object],
        frame: pd.DataFrame,
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict:
        final_year = frame.index[-1].year
        final_start = pd.Timestamp(
            year=final_year, month=1, day=1, tz="UTC"
        )
        final_frame = frame.loc[frame.index >= final_start]
        pre_final = frame.loc[frame.index < final_start]
        if len(final_frame) < 300 or len(pre_final) < 365 * 8:
            raise ValueError(
                "10-year Bitstamp standard lacks an untouched final year"
            )
        candidates = _parameter_candidates(spec)
        attempted_variants = len(candidates)
        folds: list[JsonDict] = []
        fold_scores: dict[str, list[float]] = {}
        first_year = frame.index[0].year
        test_year = first_year + 3
        fold_number = 0
        while test_year < final_year:
            fold_number += 1
            train_end = pd.Timestamp(
                year=test_year, month=1, day=1, tz="UTC"
            )
            test_start = train_end + pd.Timedelta(days=1)
            test_end = pd.Timestamp(
                year=test_year + 1, month=1, day=1, tz="UTC"
            )
            train = frame.loc[frame.index < train_end]
            test = frame.loc[
                (frame.index >= test_start) & (frame.index < test_end)
            ]
            if train.empty or test.empty:
                test_year += 1
                continue
            progress(
                f"walk_forward_{fold_number}",
                min(68, 52 + fold_number * 3),
            )
            best_params: JsonDict | None = None
            best_train_sharpe = float("-inf")
            for candidate in candidates:
                self._check_cancelled(cancelled)
                candidate_spec = _with_parameters(spec, candidate)
                train_result = self._run(
                    candidate_spec,
                    train,
                    build_strategy(candidate_spec),
                )
                train_sharpe = _float_value(_metrics(train_result)["sharpe"])
                if train_sharpe > best_train_sharpe:
                    best_train_sharpe = train_sharpe
                    best_params = candidate
            assert best_params is not None
            selected_spec = _with_parameters(spec, best_params)
            test_metrics = _metrics(
                self._run(
                    selected_spec,
                    test,
                    build_strategy(selected_spec),
                )
            )
            key = json.dumps(best_params, sort_keys=True)
            fold_scores.setdefault(key, []).append(
                _float_value(test_metrics["sharpe"])
            )
            folds.append(
                {
                    "train_start": train.index[0].isoformat(),
                    "train_end": train.index[-1].isoformat(),
                    "test_start": test.index[0].isoformat(),
                    "test_end": test.index[-1].isoformat(),
                    "purge_bars": 1,
                    "selected_parameters": best_params,
                    "train_sharpe": best_train_sharpe,
                    "test_metrics": test_metrics,
                }
            )
            test_year += 1
        if not fold_scores:
            raise ValueError("standard validation could not create walk-forward folds")
        final_key = max(
            fold_scores,
            key=lambda key: (
                sum(fold_scores[key]) / len(fold_scores[key]),
                key,
            ),
        )
        selected_parameters = json.loads(final_key)
        selected_spec = _with_parameters(spec, selected_parameters)

        progress("parameter_sensitivity", 70)
        sensitivity: list[JsonDict] = []
        for candidate in candidates:
            self._check_cancelled(cancelled)
            candidate_spec = _with_parameters(spec, candidate)
            metrics = _metrics(
                self._run(
                    candidate_spec,
                    pre_final,
                    build_strategy(candidate_spec),
                )
            )
            sensitivity.append(
                {"parameters": candidate, "metrics": metrics}
            )

        progress("final_untouched_test", 75)
        final_result = self._run(
            selected_spec,
            final_frame,
            build_strategy(selected_spec),
        )
        final_metrics = _metrics(final_result)
        final_returns = _equity_series(final_result).pct_change().dropna()
        final_buy_hold_metrics = _metrics(
            self._run(
                selected_spec,
                final_frame,
                default_strategy_registry().create("buy_and_hold", {}),
            )
        )

        monte_carlo = self._batched_monte_carlo(
            final_returns,
            block_size=min(20, max(2, len(final_returns) // 10)),
            seed=_integer_value(spec.get("seed"), 7),
            progress=progress,
            cancelled=cancelled,
        )
        self._check_cancelled(cancelled)

        progress("cost_stress", 84)
        cost_stress: JsonDict = {}
        for multiplier in (1, 2, 3):
            self._check_cancelled(cancelled)
            stressed_spec = _with_cost_multiplier(selected_spec, multiplier)
            stressed = self._run(
                stressed_spec,
                final_frame,
                build_strategy(stressed_spec),
            )
            cost_stress[f"{multiplier}x"] = _metrics(stressed)

        progress("future_data_consistency", 86)
        lookahead_consistent = self._future_data_consistent(
            selected_spec,
            pre_final,
            frame,
        )
        self._check_cancelled(cancelled)
        progress("recursive_stability", 88)
        recursive_stable = self._recursive_stable(
            selected_spec,
            frame,
        )
        return {
            "data_complete": True,
            "lookahead_consistent": lookahead_consistent,
            "recursive_stable": recursive_stable,
            "walk_forward": {
                "folds": folds,
                "final_test_start": final_start.isoformat(),
                "selected_parameters": selected_parameters,
            },
            "sensitivity": {
                "attempted_variants": attempted_variants,
                "evaluations": sensitivity,
            },
            "out_of_sample": {
                "sharpe": _float_value(final_metrics["sharpe"]),
                "total_return": _float_value(final_metrics["total_return"]),
                "max_drawdown": _float_value(final_metrics["max_drawdown"]),
            },
            "cost_stress": cost_stress,
            "deflated_sharpe_probability": deflated_sharpe_probability(
                final_returns,
                attempted_variants=attempted_variants,
            ),
            "monte_carlo": monte_carlo,
            "benchmarks": {
                "buy_and_hold": {
                    "total_return": _float_value(
                        final_buy_hold_metrics["total_return"]
                    ),
                    "max_drawdown": _float_value(
                        final_buy_hold_metrics["max_drawdown"]
                    ),
                }
            },
            "profile_complete": True,
        }

    @staticmethod
    def _batched_monte_carlo(
        returns: pd.Series,
        *,
        block_size: int,
        seed: int,
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict:
        paths: list[tuple[float, ...]] = []
        for batch in range(5):
            ResearchExecutor._check_cancelled(cancelled)
            progress(f"monte_carlo_{batch + 1}_of_5", 78 + batch)
            sampled = BlockBootstrap.run(
                returns,
                simulations=100,
                block_size=block_size,
                seed=seed + batch,
            )
            paths.extend(sampled.paths)
        cumulative = np.asarray(
            [
                np.concatenate(
                    (
                        np.asarray([1.0]),
                        np.cumprod(1 + np.asarray(path, dtype="float64")),
                    )
                )
                for path in paths
            ],
            dtype="float64",
        )
        final = cumulative[:, -1] - 1
        return {
            "simulations": 500,
            "block_size": block_size,
            "seed": seed,
            "loss_probability": float(np.mean(final < 0)),
            "return_p05": float(np.quantile(final, 0.05)),
            "return_p50": float(np.quantile(final, 0.50)),
            "return_p95": float(np.quantile(final, 0.95)),
            "percentile_paths": {
                "p05": np.quantile(cumulative, 0.05, axis=0).tolist(),
                "p50": np.quantile(cumulative, 0.50, axis=0).tolist(),
                "p95": np.quantile(cumulative, 0.95, axis=0).tolist(),
            },
        }

    @staticmethod
    def _quick_evidence(
        primary_metrics: Mapping[str, object],
        buy_hold_metrics: Mapping[str, object],
    ) -> JsonDict:
        # Quick runs are diagnostic and deliberately fail robustness gates.
        return {
            "data_complete": True,
            "lookahead_consistent": None,
            "recursive_stable": None,
            "out_of_sample": {
                "sharpe": _float_value(primary_metrics["sharpe"]),
                "total_return": _float_value(primary_metrics["total_return"]),
                "max_drawdown": _float_value(primary_metrics["max_drawdown"]),
            },
            "cost_stress": {
                "2x": {
                    "total_return": _float_value(
                        primary_metrics["total_return"]
                    )
                }
            },
            "deflated_sharpe_probability": 0.0,
            "monte_carlo": {"loss_probability": 1.0},
            "benchmarks": {
                "buy_and_hold": {
                    "total_return": _float_value(
                        buy_hold_metrics["total_return"]
                    ),
                    "max_drawdown": _float_value(
                        buy_hold_metrics["max_drawdown"]
                    ),
                }
            },
            "profile_complete": False,
        }

    def _future_data_consistent(
        self,
        spec: Mapping[str, object],
        prefix: pd.DataFrame,
        extended: pd.DataFrame,
    ) -> bool:
        prefix_result = self._run(spec, prefix, build_strategy(spec))
        extended_result = self._run(spec, extended, build_strategy(spec))
        cutoff = prefix.index[-1].to_pydatetime()
        prefix_fills = tuple(
            _fill_signature(fill)
            for fill in prefix_result.fills
        )
        extended_fills = tuple(
            _fill_signature(fill)
            for fill in extended_result.fills
            if fill.timestamp <= cutoff
        )
        return prefix_fills == extended_fills

    def _recursive_stable(
        self,
        spec: Mapping[str, object],
        frame: pd.DataFrame,
    ) -> bool:
        full = self._run(spec, frame, build_strategy(spec))
        shifted_frame = frame.iloc[min(30, len(frame) // 10):]
        shifted = self._run(spec, shifted_frame, build_strategy(spec))
        full_returns = _equity_series(full).pct_change()
        shifted_returns = _equity_series(shifted).pct_change()
        overlap = full_returns.index.intersection(shifted_returns.index)
        if len(overlap) < 30:
            return False
        left = full_returns.loc[overlap].iloc[30:].fillna(0).to_numpy()
        right = shifted_returns.loc[overlap].iloc[30:].fillna(0).to_numpy()
        return bool(np.allclose(left, right, rtol=1e-7, atol=1e-9))

    def _load_frame(self, dataset_id: str) -> pd.DataFrame:
        frame = pd.read_parquet(self.datasets.path_for(dataset_id))
        frame = frame.sort_index()
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        else:
            frame.index = frame.index.tz_convert("UTC")
        return frame

    def _run(
        self,
        research_spec: Mapping[str, object],
        frame: pd.DataFrame,
        strategy: Strategy,
    ) -> BacktestResult:
        dataset = self.datasets.get(str(research_spec["dataset_id"]))
        timeframe = str(dataset["timeframe"])
        if timeframe not in {"1h", "1d"}:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        request = _request_for(
            frame,
            timeframe=timeframe,
            synthetic=False,
        )
        assumptions = research_spec.get("assumptions")
        if not isinstance(assumptions, Mapping):
            raise ValueError("assumptions must be an object")
        from btc_backtest.engine.models import BacktestSpec

        strategy_parameters = research_spec.get("strategy_params")
        spec = BacktestSpec(
            strategy=strategy.metadata.id,
            strategy_params=(
                dict(strategy_parameters)
                if isinstance(strategy_parameters, Mapping)
                else {}
            ),
            data=request,
            initial_cash=Decimal(str(assumptions["initial_cash"])),
            fee_bps=Decimal(str(assumptions["fee_bps"])),
            slippage_bps=Decimal(str(assumptions["slippage_bps"])),
        )
        market = _market_dataset(
            "spot",
            frame,
            request=request,
            real_data=True,
            source=str(dataset["source"]),
        )
        return EventRunner().run(
            spec,
            MarketBundle(primary=market, auxiliary={}),
            strategy,
        )

    def _write_artifacts(
        self,
        summary: JsonDict,
        result: BacktestResult,
        buy_hold: BacktestResult,
        fixed_dca: BacktestResult,
    ) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        run_id = str(summary["run_id"])
        target = self.artifact_root / run_id
        if target.exists():
            raise FileExistsError(f"research run already exists: {run_id}")
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{run_id}-", dir=self.artifact_root)
        )
        try:
            equity = pd.concat(
                (
                    _equity_series(result).rename("strategy"),
                    _equity_series(buy_hold).rename("buy_and_hold"),
                    _equity_series(fixed_dca).rename("fixed_dca"),
                ),
                axis=1,
            )
            equity.to_csv(temporary / "equity.csv", index_label="timestamp")
            _research_trades_frame(result).to_csv(
                temporary / "trades.csv",
                index=False,
            )
            summary_data = summary.get("data")
            data_manifest = {
                "schema_version": "1",
                **(
                    dict(summary_data)
                    if isinstance(summary_data, Mapping)
                    else {}
                ),
            }
            _write_json(temporary / "data_manifest.json", data_manifest)
            checksums = {
                name: _sha256(temporary / name)
                for name in ("data_manifest.json", "equity.csv", "trades.csv")
            }
            summary["checksums"] = checksums
            _write_json(temporary / "summary.json", summary)
            os.replace(temporary, target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def _validate_frame(frame: pd.DataFrame) -> None:
        required = {"open", "high", "low", "close", "volume"}
        missing = required.difference(frame.columns)
        if frame.empty:
            raise ValueError("dataset contains no OHLCV rows")
        if missing:
            raise ValueError(f"dataset is missing columns: {sorted(missing)}")
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise ValueError("dataset index must be a DatetimeIndex")
        if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
            raise ValueError("dataset timestamps must be ordered and unique")
        if frame[list(required)].isna().any().any():
            raise ValueError("dataset contains missing OHLCV values")

    @staticmethod
    def _check_cancelled(cancelled: CancellationCheck) -> None:
        if cancelled():
            from qt.research.worker import ResearchCancelledError

            raise ResearchCancelledError("research job was cancelled")


def _metrics(result: BacktestResult) -> JsonDict:
    return _json_safe(
        compute_metrics(result).model_dump(mode="json")
    )  # type: ignore[return-value]


def _json_safe(value: object) -> object:
    return json.loads(
        json.dumps(value, default=str, allow_nan=False, sort_keys=True)
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _with_parameters(
    spec: Mapping[str, object],
    parameters: Mapping[str, object],
) -> JsonDict:
    updated = dict(spec)
    updated["strategy_params"] = dict(parameters)
    return updated


def _with_cost_multiplier(
    spec: Mapping[str, object],
    multiplier: int,
) -> JsonDict:
    updated = dict(spec)
    assumptions = spec.get("assumptions")
    if not isinstance(assumptions, Mapping):
        raise ValueError("assumptions must be an object")
    updated["assumptions"] = {
        **dict(assumptions),
        "fee_bps": _float_value(assumptions["fee_bps"]) * multiplier,
        "slippage_bps": _float_value(assumptions["slippage_bps"]) * multiplier,
    }
    return updated


def _parameter_candidates(spec: Mapping[str, object]) -> list[JsonDict]:
    if spec.get("mode") != "template":
        strategy_parameters = spec.get("strategy_params")
        return [
            dict(strategy_parameters)
            if isinstance(strategy_parameters, Mapping)
            else {}
        ]
    strategy_id = str(spec.get("strategy_id", ""))
    grids: dict[str, list[JsonDict]] = {
        "buy_and_hold": [
            {"allocation": value} for value in ("0.8", "0.9", "1")
        ],
        "fixed_dca": [
            {"quote_amount": value} for value in ("50", "100", "200")
        ],
        "smart_dca": [
            {"rsi_window": window, "rsi_oversold": entry}
            for window in (10, 14, 21)
            for entry in ("25", "30", "35")
        ],
        "sma_crossover": [
            {"fast_window": fast, "slow_window": slow}
            for fast in (20, 50, 100)
            for slow in (150, 200, 300)
            if fast < slow
        ],
        "rsi_mean_reversion": [
            {"window": window, "entry": entry, "exit": "55"}
            for window in (10, 14, 21)
            for entry in ("25", "30", "35")
        ],
        "bollinger_mean_reversion": [
            {"window": window, "stddev": deviation}
            for window in (15, 20, 30)
            for deviation in ("1.5", "2", "2.5")
        ],
        "donchian_breakout": [
            {"entry_window": entry, "exit_window": exit_window}
            for entry in (20, 55, 100)
            for exit_window in (10, 20, 50)
            if exit_window < entry
        ],
    }
    candidates = grids.get(strategy_id)
    if candidates is None:
        base = spec.get("strategy_params")
        return [dict(base) if isinstance(base, Mapping) else {}]
    return candidates[:25]


def _fill_signature(fill: Fill) -> tuple[str, str, str, str, str]:
    return (
        fill.timestamp.isoformat(),
        fill.side.value,
        str(fill.quantity),
        str(fill.price),
        str(fill.fee),
    )


def _research_trades_frame(result: BacktestResult) -> pd.DataFrame:
    realized_by_close = {
        trade.closed_at: float(trade.realized_pnl)
        for trade in result.trades
    }
    opened_at: pd.Timestamp | None = None
    rows: list[JsonDict] = []
    for fill in result.fills:
        timestamp = pd.Timestamp(fill.timestamp)
        holding_hours: float | None = None
        if fill.side.value == "buy" and opened_at is None:
            opened_at = timestamp
        elif fill.side.value == "sell" and opened_at is not None:
            holding_hours = (
                timestamp - opened_at
            ).total_seconds() / 3600
            opened_at = None
        rows.append(
            {
                "ts": fill.timestamp,
                "side": fill.side.value,
                "qty": float(fill.quantity),
                "price": float(fill.price),
                "fee": float(fill.fee),
                "pnl": realized_by_close.get(fill.timestamp, 0.0),
                "reason": fill.reason,
                "holding_hours": holding_hours,
            }
        )
    if rows:
        return pd.DataFrame(rows)
    return _trades_frame(result).assign(
        reason="closed_trade",
        holding_hours=None,
    )


def _float_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(
        value, int | float | str | Decimal
    ):
        raise ValueError("numeric research evidence is invalid")
    result = float(value)
    if not np.isfinite(result):
        raise ValueError("numeric research evidence must be finite")
    return result


def _integer_value(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError("integer research configuration is invalid")
    return int(value)
