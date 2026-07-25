"""Deterministic stress scenarios for backtest robustness checks."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
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


class BacktestExecutor(Protocol):
    def run(self, spec: BacktestSpec) -> BacktestResult: ...


class StressScenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    parameters: Mapping[str, object] = Field(default_factory=dict)

    @field_validator("parameters")
    @classmethod
    def freeze_parameters(
        cls,
        value: Mapping[str, object],
    ) -> Mapping[str, object]:
        return MappingProxyType(dict(value))

    @field_serializer("parameters")
    def serialize_parameters(
        self,
        value: Mapping[str, object],
    ) -> dict[str, object]:
        return dict(value)


class CostStress(BaseModel):
    model_config = ConfigDict(frozen=True)

    fee_multiplier: Decimal = Field(gt=0)
    slippage_multiplier: Decimal = Field(gt=0)

    @field_validator("fee_multiplier", "slippage_multiplier")
    @classmethod
    def validate_multiplier(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("cost stress multipliers must be finite")
        return value

    @property
    def scenario(self) -> StressScenario:
        return StressScenario(
            id="cost",
            parameters={
                "fee_multiplier": str(self.fee_multiplier),
                "slippage_multiplier": str(self.slippage_multiplier),
            },
        )


class ProviderOutageStress(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str = Field(min_length=1)
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider outage timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_interval(self) -> ProviderOutageStress:
        if self.end <= self.start:
            raise ValueError("provider outage end must be after start")
        return self

    @property
    def scenario(self) -> StressScenario:
        return StressScenario(
            id="provider_outage",
            parameters={
                "provider": self.provider,
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
            },
        )


class ExecutionDelayStress(BaseModel):
    model_config = ConfigDict(frozen=True)

    bars: int = Field(gt=0)

    @property
    def scenario(self) -> StressScenario:
        return StressScenario(
            id="execution_delay",
            parameters={"bars": self.bars},
        )


class MissingBarStress(BaseModel):
    model_config = ConfigDict(frozen=True)

    every_n_bars: int = Field(gt=1)

    @property
    def scenario(self) -> StressScenario:
        return StressScenario(
            id="missing_bars",
            parameters={"every_n_bars": self.every_n_bars},
        )


StressInput = (
    CostStress
    | ProviderOutageStress
    | ExecutionDelayStress
    | MissingBarStress
)


class StressMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str
    final_equity: Decimal
    metrics: PerformanceMetrics
    run_id: str
    data_fingerprints: tuple[str, ...]

    @field_validator("final_equity")
    @classmethod
    def validate_final_equity(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("final equity must be finite")
        return value


class StressEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_metrics: StressMetric
    scenario_metrics: tuple[StressMetric, ...]
    scenarios: tuple[StressScenario, ...]


class StressRunner:
    def __init__(self, runner: BacktestExecutor) -> None:
        self._runner = runner

    def run(
        self,
        base_spec: BacktestSpec,
        scenarios: tuple[StressInput, ...],
    ) -> StressEvaluation:
        if not scenarios:
            raise ValueError("stress validation requires scenarios")
        base_result = self._runner.run(base_spec)
        scenario_results: list[StressMetric] = []
        scenario_metadata: list[StressScenario] = []
        for scenario in scenarios:
            applied = _apply_scenario(base_spec, scenario)
            result = self._runner.run(applied)
            metadata = scenario.scenario
            scenario_metadata.append(metadata)
            scenario_results.append(_metric(metadata.id, result))
        return StressEvaluation(
            base_metrics=_metric("base", base_result),
            scenario_metrics=tuple(scenario_results),
            scenarios=tuple(scenario_metadata),
        )


def _apply_scenario(
    spec: BacktestSpec,
    scenario: StressInput,
) -> BacktestSpec:
    if isinstance(scenario, CostStress):
        return spec.model_copy(
            update={
                "fee_bps": spec.fee_bps * scenario.fee_multiplier,
                "slippage_bps": (
                    spec.slippage_bps * scenario.slippage_multiplier
                ),
            }
        )
    return spec


def _metric(scenario_id: str, result: BacktestResult) -> StressMetric:
    if not result.snapshots:
        raise ValueError("stress result requires snapshots")
    return StressMetric(
        scenario_id=scenario_id,
        final_equity=result.snapshots[-1].equity,
        metrics=compute_metrics(result),
        run_id=result.run_id,
        data_fingerprints=tuple(
            manifest.normalized_sha256
            for manifest in result.data_manifests
        ),
    )
