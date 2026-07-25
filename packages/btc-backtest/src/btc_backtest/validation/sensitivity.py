"""Parameter sensitivity grids and multiple-testing diagnostics."""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from btc_backtest.engine.models import BacktestResult, BacktestSpec
from btc_backtest.reporting.metrics import PerformanceMetrics, compute_metrics


class BacktestExecutor(Protocol):
    def run(self, spec: BacktestSpec) -> BacktestResult: ...


class MultipleTestingDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw_p_values: tuple[float, ...]
    adjusted_p_values: tuple[float, ...]
    method: str
    attempted_variants: int = Field(ge=0)

    @field_validator("raw_p_values", "adjusted_p_values")
    @classmethod
    def validate_p_values(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        if any(value < 0 or value > 1 for value in values):
            raise ValueError("p-values must be between 0 and 1")
        return values


class SensitivityEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    parameters: Mapping[str, object]
    metrics: PerformanceMetrics
    run_id: str
    data_fingerprints: tuple[str, ...]

    @field_validator("parameters")
    @classmethod
    def freeze_parameters(
        cls,
        value: Mapping[str, object],
    ) -> Mapping[str, object]:
        copied = dict(value)
        _canonical_json(copied)
        return MappingProxyType(copied)

    @field_serializer("parameters")
    def serialize_parameters(
        self,
        value: Mapping[str, object],
    ) -> dict[str, object]:
        return dict(value)


class SensitivityResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluations: tuple[SensitivityEvaluation, ...]
    best: SensitivityEvaluation
    multiple_testing: MultipleTestingDiagnostic


class SensitivityAnalyzer:
    def __init__(self, runner: BacktestExecutor) -> None:
        self._runner = runner

    def run(
        self,
        base_spec: BacktestSpec,
        grid: Mapping[str, Iterable[object]],
    ) -> SensitivityResult:
        candidates = _grid_candidates(grid)
        evaluations: list[SensitivityEvaluation] = []
        for parameters in candidates:
            spec = base_spec.model_copy(
                update={"strategy_params": dict(parameters)}
            )
            result = self._runner.run(spec)
            evaluations.append(
                SensitivityEvaluation(
                    parameters=parameters,
                    metrics=compute_metrics(result),
                    run_id=result.run_id,
                    data_fingerprints=_data_fingerprints(result),
                )
            )
        ordered = tuple(evaluations)
        best = sorted(
            ordered,
            key=lambda item: (
                -item.metrics.total_return,
                abs(item.metrics.max_drawdown),
                item.metrics.turnover,
                _canonical_json(item.parameters),
            ),
        )[0]
        diagnostic = multiple_testing(
            [
                _p_value_from_return(item.metrics.total_return)
                for item in ordered
            ],
            method="holm",
        )
        return SensitivityResult(
            evaluations=ordered,
            best=best,
            multiple_testing=diagnostic,
        )


def multiple_testing(
    p_values: Iterable[float],
    *,
    method: str,
) -> MultipleTestingDiagnostic:
    raw = tuple(float(value) for value in p_values)
    if method != "holm":
        raise ValueError("multiple testing method must be holm")
    if any(value < 0 or value > 1 for value in raw):
        raise ValueError("p-values must be between 0 and 1")
    m = len(raw)
    indexed = sorted(enumerate(raw), key=lambda item: item[1])
    adjusted = [0.0 for _ in raw]
    prior = 0.0
    for rank, (index, value) in enumerate(indexed):
        candidate = min(1.0, value * (m - rank))
        prior = max(prior, candidate)
        adjusted[index] = prior
    return MultipleTestingDiagnostic(
        raw_p_values=raw,
        adjusted_p_values=tuple(adjusted),
        method=method,
        attempted_variants=m,
    )


def _grid_candidates(
    grid: Mapping[str, Iterable[object]],
) -> tuple[Mapping[str, object], ...]:
    if not grid:
        raise ValueError("sensitivity grid must be non-empty")
    keys = tuple(sorted(grid))
    values = tuple(
        tuple(sorted(grid[key], key=_value_sort_key))
        for key in keys
    )
    if any(not items for items in values):
        raise ValueError("sensitivity grid values must be non-empty")
    candidates = [
        MappingProxyType(dict(zip(keys, combination, strict=True)))
        for combination in itertools.product(*values)
    ]
    return tuple(candidates)


def _value_sort_key(value: object) -> tuple[str, Decimal | str]:
    if isinstance(value, bool):
        return ("bool", str(value))
    if isinstance(value, int | float | Decimal):
        return ("number", Decimal(str(value)))
    return (type(value).__name__, str(value))


def _p_value_from_return(total_return: float) -> float:
    return max(0.0, min(1.0, 1.0 - total_return))


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
            "sensitivity parameters must be JSON serializable"
        ) from error
