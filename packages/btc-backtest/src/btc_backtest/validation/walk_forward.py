"""Deterministic walk-forward parameter selection."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from statistics import mean
from types import MappingProxyType
from typing import Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from btc_backtest.engine.models import BacktestResult, BacktestSpec
from btc_backtest.reporting.metrics import PerformanceMetrics, compute_metrics
from btc_backtest.validation.models import (
    ValidationSpec,
    ValidationSplit,
    Window,
)


class BacktestExecutor(Protocol):
    def run(self, spec: BacktestSpec) -> BacktestResult: ...


class ParameterCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    parameters: Mapping[str, object] = Field(
        default_factory=dict,
        validate_default=True,
    )

    @field_validator("parameters")
    @classmethod
    def freeze_parameters(
        cls,
        value: Mapping[str, object],
    ) -> Mapping[str, object]:
        copied = dict(value)
        if any(not isinstance(key, str) for key in copied):
            raise ValueError("candidate parameter keys must be strings")
        _canonical_json(copied)
        return MappingProxyType(copied)

    @field_serializer("parameters")
    def serialize_parameters(
        self,
        value: Mapping[str, object],
    ) -> dict[str, object]:
        return dict(value)

    @property
    def canonical_key(self) -> str:
        return _canonical_json(self.parameters)


class WindowEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    split: ValidationSplit
    selected_on: Window
    scored_on: Window
    selected_candidate: ParameterCandidate
    selected_metrics: PerformanceMetrics
    test_metrics: PerformanceMetrics
    train_run_ids: tuple[str, ...]
    test_run_id: str
    child_data_fingerprints: tuple[str, ...]

    @model_validator(mode="after")
    def validate_windows(self) -> WindowEvaluation:
        if self.selected_on != self.split.train:
            raise ValueError("window selection must use the train window")
        if self.scored_on != self.split.test:
            raise ValueError("window scoring must use the adjacent test window")
        return self


class FinalTestEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    selected_candidate: ParameterCandidate
    scored_on: Window
    metrics: PerformanceMetrics
    run_id: str
    child_data_fingerprints: tuple[str, ...]


class WalkForwardResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    spec: ValidationSpec
    windows: tuple[WindowEvaluation, ...]
    final_evaluation: FinalTestEvaluation | None = None
    selected_parameters: tuple[Mapping[str, object], ...]
    child_run_ids: tuple[str, ...]
    child_data_fingerprints: tuple[str, ...]

    @field_validator("selected_parameters")
    @classmethod
    def freeze_selected_parameters(
        cls,
        value: tuple[Mapping[str, object], ...],
    ) -> tuple[Mapping[str, object], ...]:
        return tuple(MappingProxyType(dict(item)) for item in value)

    @field_serializer("selected_parameters")
    def serialize_selected_parameters(
        self,
        value: tuple[Mapping[str, object], ...],
    ) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in value)


class WalkForwardValidator:
    def __init__(
        self,
        runner: BacktestExecutor,
        spec: ValidationSpec,
        *,
        splits: tuple[ValidationSplit, ...],
    ) -> None:
        if not splits:
            raise ValueError("walk-forward validation requires at least one split")
        self._runner = runner
        self._spec = spec
        self._splits = splits

    def run(
        self,
        spec: BacktestSpec,
        candidates: Iterable[ParameterCandidate],
    ) -> WalkForwardResult:
        ordered = tuple(
            sorted(candidates, key=lambda candidate: candidate.canonical_key)
        )
        if not ordered:
            raise ValueError("walk-forward validation requires candidates")

        windows: list[WindowEvaluation] = []
        all_run_ids: list[str] = []
        all_fingerprints: list[str] = []
        for split in self._splits:
            train_evaluations: list[
                tuple[ParameterCandidate, PerformanceMetrics, BacktestResult]
            ] = []
            for candidate in ordered:
                train_result = self._run_child(
                    spec,
                    candidate,
                    split.train,
                )
                train_metrics = compute_metrics(train_result)
                train_evaluations.append(
                    (candidate, train_metrics, train_result)
                )
            selected, selected_metrics, _ = _select_candidate(
                train_evaluations,
                objective=self._spec.objective,
            )
            test_result = self._run_child(spec, selected, split.test)
            test_metrics = compute_metrics(test_result)
            train_run_ids = tuple(
                result.run_id
                for _, _, result in train_evaluations
            )
            fingerprints = tuple(
                _fingerprint
                for _, _, result in train_evaluations
                for _fingerprint in _data_fingerprints(result)
            ) + _data_fingerprints(test_result)
            all_run_ids.extend((*train_run_ids, test_result.run_id))
            all_fingerprints.extend(fingerprints)
            windows.append(
                WindowEvaluation(
                    split=split,
                    selected_on=split.train,
                    scored_on=split.test,
                    selected_candidate=selected,
                    selected_metrics=selected_metrics,
                    test_metrics=test_metrics,
                    train_run_ids=train_run_ids,
                    test_run_id=test_result.run_id,
                    child_data_fingerprints=fingerprints,
                )
            )

        final_candidate = _select_final_candidate(
            windows,
            objective=self._spec.objective,
        )
        final_window = Window(
            start=self._spec.final_test_start,
            end=self._spec.final_test_end,
            timestamps=(),
        )
        final_result = self._run_child(spec, final_candidate, final_window)
        final_metrics = compute_metrics(final_result)
        final_fingerprints = _data_fingerprints(final_result)
        all_run_ids.append(final_result.run_id)
        all_fingerprints.extend(final_fingerprints)

        return WalkForwardResult(
            spec=self._spec,
            windows=tuple(windows),
            final_evaluation=FinalTestEvaluation(
                selected_candidate=final_candidate,
                scored_on=final_window,
                metrics=final_metrics,
                run_id=final_result.run_id,
                child_data_fingerprints=final_fingerprints,
            ),
            selected_parameters=tuple(
                item.selected_candidate.parameters
                for item in windows
            ),
            child_run_ids=tuple(all_run_ids),
            child_data_fingerprints=tuple(all_fingerprints),
        )

    def _run_child(
        self,
        spec: BacktestSpec,
        candidate: ParameterCandidate,
        window: Window,
    ) -> BacktestResult:
        parameters = dict(spec.strategy_params)
        parameters.update(candidate.parameters)
        return self._runner.run(
            spec.model_copy(
                update={
                    "strategy_params": parameters,
                    "data": spec.data.model_copy(
                        update={
                            "start": window.start,
                            "end": window.end,
                        }
                    ),
                }
            )
        )


def _select_candidate(
    evaluations: list[tuple[ParameterCandidate, PerformanceMetrics, BacktestResult]],
    *,
    objective: str,
) -> tuple[ParameterCandidate, PerformanceMetrics, BacktestResult]:
    ranked = sorted(
        evaluations,
        key=lambda item: (
            -_objective(item[1], objective),
            abs(item[1].max_drawdown),
            item[1].turnover,
            item[0].canonical_key,
        ),
    )
    return ranked[0]


def _select_final_candidate(
    windows: list[WindowEvaluation],
    *,
    objective: str,
) -> ParameterCandidate:
    by_key: dict[str, list[WindowEvaluation]] = {}
    by_candidate: dict[str, ParameterCandidate] = {}
    for item in windows:
        key = item.selected_candidate.canonical_key
        by_key.setdefault(key, []).append(item)
        by_candidate[key] = item.selected_candidate
    ranked = sorted(
        by_key.items(),
        key=lambda item: (
            -mean(
                _objective(window.test_metrics, objective)
                for window in item[1]
            ),
            mean(abs(window.test_metrics.max_drawdown) for window in item[1]),
            mean(window.test_metrics.turnover for window in item[1]),
            item[0],
        ),
    )
    return by_candidate[ranked[0][0]]


def _objective(metrics: PerformanceMetrics, field: str) -> float:
    value = getattr(metrics, field, None)
    if not isinstance(value, int | float):
        raise ValueError(f"unsupported walk-forward objective: {field}")
    return float(value)


def _data_fingerprints(result: BacktestResult) -> tuple[str, ...]:
    return tuple(
        manifest.normalized_sha256
        for manifest in result.data_manifests
    )


def _canonical_json(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except TypeError as error:
        raise ValueError(
            "candidate parameters must be JSON serializable"
        ) from error
