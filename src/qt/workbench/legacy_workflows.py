"""Read-only, JSON-safe access to the original Qt5 gallery workflows.

This module deliberately invokes only ``qt.strategy_ports.gallery``.  It does
not call source ``fetch_data()``, the strategy runner, scanners, providers,
risk engine, paper broker, or a live broker.  Those retained workflows own
network access and/or mutable state and are outside research evaluation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import MISSING, asdict, fields
from datetime import datetime
from decimal import Decimal
from typing import Literal

import pandas as pd
from pandas import DataFrame, Series

from qt.strategies.base import EvaluationResult, Opportunity
from qt.strategies.capitulation import CapitulationParams
from qt.strategies.carry import CarryParams
from qt.strategies.dca import DCAParams
from qt.strategies.sim.base import StrategyResult
from qt.strategies.sim.basis_carry import BasisCarryConfig
from qt.strategies.sim.smart_dca import SmartDCAConfig
from qt.strategies.sim.trend_weekly import WeeklyTrendConfig
from qt.strategies.sim.wick_catcher import WickCatcherConfig
from qt.strategies.trend import TrendParams
from qt.strategies.wick_catcher import WickCatcherParams
from qt.strategy_ports.btcqt import DataVersion
from qt.strategy_ports.gallery import (
    GALLERY_PORTS,
    GalleryCausalInput,
    GalleryLiveDecision,
    GalleryLivePort,
    GalleryPortDataError,
    GalleryPortMetadata,
    GallerySimulationDecision,
    GallerySimulationPort,
    create_gallery_port,
)

ExecutionMode = Literal["research"]


class LegacyWorkflowSafetyError(ValueError):
    """A caller attempted a workflow outside the immutable research boundary."""


def legacy_workflow_catalog() -> list[dict[str, object]]:
    """Return JSON-safe schema/semantic metadata for all nine source workflows."""
    return [_workflow_definition(metadata) for metadata in GALLERY_PORTS.values()]


def legacy_workflow_definition(workflow_id: str) -> dict[str, object]:
    """Return the exact input/result contract for one source workflow."""
    metadata = _metadata(workflow_id)
    return _workflow_definition(metadata)


def run_legacy_workflow(
    workflow_id: str,
    *,
    data: Mapping[str, object],
    decision_at: datetime,
    available_at: datetime,
    input_versions: Sequence[DataVersion],
    parameters: Mapping[str, object] | None = None,
    execution_mode: ExecutionMode | str = "research",
) -> dict[str, object]:
    """Evaluate/replay one original workflow from immutable local snapshots.

    Data frames and series are copied by ``GalleryCausalInput`` and clipped to
    ``decision_at`` before the source class receives them.  Output contains no
    native order intent: live results become manual proposals only.
    """
    _require_research_mode(execution_mode)
    metadata = _metadata(workflow_id)
    context = GalleryCausalInput(
        data=data,
        decision_at=decision_at,
        available_at=available_at,
        inputs=tuple(input_versions),
    )
    port = create_gallery_port(workflow_id, parameters=parameters)
    if isinstance(port, GalleryLivePort):
        live_decision = port.evaluate(context)
        return {
            **_live_payload(live_decision),
            "causal_decision_at": _timestamp(decision_at),
            "causal_available_at": _timestamp(available_at),
        }
    if isinstance(port, GallerySimulationPort):
        simulation_decision = port.run(context)
        return {
            **_simulation_payload(simulation_decision),
            "causal_decision_at": _timestamp(decision_at),
            "causal_available_at": _timestamp(available_at),
        }
    raise LegacyWorkflowSafetyError(f"unsupported source workflow interface: {metadata.interface}")


def _workflow_definition(metadata: GalleryPortMetadata) -> dict[str, object]:
    return {
        "id": metadata.strategy_id,
        "source": _json_value(asdict(metadata.source)),
        "interface": metadata.interface,
        "input_schema": {
            "execution_mode": {"const": "research", "description": "live/paper mutation is rejected"},
            "data": {
                "required": [dependency.dataset_id for dependency in metadata.required_data],
                "optional": [dependency.dataset_id for dependency in metadata.optional_data],
                "requirements": {
                    dependency.dataset_id: dependency.reason
                    for dependency in (*metadata.required_data, *metadata.optional_data)
                },
            },
            "decision_at": "timezone-aware ISO-8601 timestamp",
            "available_at": "timezone-aware ISO-8601 timestamp at or before decision_at",
            "input_versions": "DataVersion for each supplied dataset",
            "parameters": _parameter_schema(metadata.strategy_id),
        },
        "result_schema": (
            {
                "evaluation": "original EvaluationResult serialized as JSON",
                "proposal": "manual operator proposal or null; never an order intent",
                "explanation": "original metrics, notes, opportunity reason, and source hook",
                "causal_decision_at": "snapshot timestamp distinct from original EvaluationResult.ts",
            }
            if metadata.interface == "live_evaluation_result"
            else {
                "replay": "original StrategyResult series, trades, and diagnostics serialized as JSON",
                "explanation": "source replay metadata and complete input provenance",
                "causal_decision_at": "snapshot timestamp used for causal clipping",
            }
        ),
        "native_hook": metadata.native_hook,
        "safety": {
            "network": "not invoked",
            "live_order": "not invoked",
            "risk_scanner_provider_workflows": "not invoked; caller supplies historical snapshots",
        },
    }


def _live_payload(decision: GalleryLiveDecision) -> dict[str, object]:
    evaluation = decision.evaluation
    return {
        "workflow_id": decision.metadata.strategy_id,
        "kind": "live_evaluation",
        "source": _json_value(asdict(decision.metadata.source)),
        "input_versions": _versions(decision.input_versions),
        "evaluation": _evaluation_payload(evaluation),
        "proposal": _manual_proposal(decision.metadata, evaluation.opportunity),
        "explanation": {
            "metrics": _json_value(evaluation.metrics),
            "notes": evaluation.notes,
            "native_hook": decision.metadata.native_hook,
            "execution": "manual_only",
        },
    }


def _simulation_payload(decision: GallerySimulationDecision) -> dict[str, object]:
    return {
        "workflow_id": decision.metadata.strategy_id,
        "kind": "simulation_replay",
        "source": _json_value(asdict(decision.metadata.source)),
        "input_versions": _versions(decision.input_versions),
        "replay": _strategy_result_payload(decision.result),
        "explanation": {
            "native_hook": decision.metadata.native_hook,
            "execution": "research_replay_only",
        },
    }


def _evaluation_payload(evaluation: EvaluationResult) -> dict[str, object]:
    return {
        "timestamp": _timestamp(evaluation.ts),
        "opportunity": _opportunity_payload(evaluation.opportunity),
        "metrics": _json_value(evaluation.metrics),
        "notes": evaluation.notes,
    }


def _manual_proposal(
    metadata: GalleryPortMetadata,
    opportunity: Opportunity | None,
) -> dict[str, object] | None:
    if opportunity is None:
        return None
    return {
        "kind": "manual_operator_proposal",
        "execution": "manual_only",
        "action": opportunity.action,
        "confidence": _json_value(opportunity.confidence),
        "reason": opportunity.reason,
        "details": _json_value(opportunity.details),
        "source_semantics": metadata.native_hook,
    }


def _opportunity_payload(opportunity: Opportunity | None) -> dict[str, object] | None:
    if opportunity is None:
        return None
    return {
        "timestamp": _timestamp(opportunity.ts),
        "action": opportunity.action,
        "confidence": _json_value(opportunity.confidence),
        "reason": opportunity.reason,
        "details": _json_value(opportunity.details),
    }


def _strategy_result_payload(result: StrategyResult) -> dict[str, object]:
    return {
        "equity": _series_payload(result.equity),
        "target_weight": _series_payload(result.target_weight),
        "short_weight": _series_payload(result.short_weight),
        "trades": _frame_payload(result.trades),
        "diagnostics": _frame_payload(result.diagnostics),
    }


def _series_payload(series: Series) -> list[dict[str, object]]:
    return [
        {"timestamp": _json_value(timestamp), "value": _json_value(value)}
        for timestamp, value in series.items()
    ]


def _frame_payload(frame: DataFrame) -> list[dict[str, object]]:
    if frame.empty:
        return []
    payload = frame.copy(deep=True).reset_index()
    index_name = str(payload.columns[0])
    records: list[dict[str, object]] = []
    for record in payload.to_dict(orient="records"):
        converted = {str(key): _json_value(value) for key, value in record.items()}
        if index_name not in {"index", "ts", "timestamp", "date"}:
            converted["index"] = converted.pop(index_name)
        records.append(converted)
    return records


def _versions(values: Sequence[DataVersion]) -> list[dict[str, object]]:
    return [
        {
            "dataset_id": value.dataset_id,
            "version": value.version,
            "available_at": _timestamp(value.available_at),
        }
        for value in values
    ]


def _metadata(workflow_id: str) -> GalleryPortMetadata:
    metadata = GALLERY_PORTS.get(workflow_id)
    if metadata is None:
        raise KeyError(f"unknown legacy workflow: {workflow_id}")
    return metadata


def _parameter_schema(workflow_id: str) -> dict[str, object]:
    live_models = {
        "qt5_smart_dca": DCAParams,
        "qt5_capitulation": CapitulationParams,
        "qt5_weekly_trend": TrendParams,
        "qt5_basis_carry": CarryParams,
        "qt5_wick_catcher": WickCatcherParams,
    }
    simulator_models = {
        "qt5_sim_smart_dca": SmartDCAConfig,
        "qt5_sim_weekly_trend": WeeklyTrendConfig,
        "qt5_sim_basis_carry": BasisCarryConfig,
        "qt5_sim_wick_catcher": WickCatcherConfig,
    }
    live_model = live_models.get(workflow_id)
    if live_model is not None:
        return {
            name: {
                "default": _json_value(field.default),
                "required": field.is_required(),
                "type": str(field.annotation),
            }
            for name, field in live_model.model_fields.items()  # type: ignore[attr-defined]
        }
    simulator_model = simulator_models.get(workflow_id)
    if simulator_model is None:
        raise KeyError(f"unknown source parameter schema: {workflow_id}")
    return {
        field.name: {
            "default": (
                _json_value(field.default)
                if field.default is not MISSING
                else "factory_default"
            ),
            "required": field.default is MISSING and field.default_factory is MISSING,
            "type": str(field.type),
        }
        for field in fields(simulator_model)
    }


def _require_research_mode(mode: ExecutionMode | str) -> None:
    if mode != "research":
        raise LegacyWorkflowSafetyError(
            "legacy workflows are immutable historical research only; live/paper mutation is not supported"
        )


def _json_value(value: object) -> object:
    if value is None or value is pd.NA or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, datetime | pd.Timestamp):
        return _timestamp(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item) for item in value]
    if isinstance(value, Series):
        return _series_payload(value)
    if isinstance(value, DataFrame):
        return _frame_payload(value)
    if pd.isna(value):
        return None
    return str(value)


def _timestamp(value: datetime | pd.Timestamp) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise GalleryPortDataError("result timestamp must be timezone-aware")
    return str(timestamp.tz_convert("UTC").isoformat().replace("+00:00", "Z"))


__all__ = [
    "ExecutionMode",
    "LegacyWorkflowSafetyError",
    "legacy_workflow_catalog",
    "legacy_workflow_definition",
    "run_legacy_workflow",
]
