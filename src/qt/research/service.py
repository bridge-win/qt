"""Typed request normalization and strategy construction for research jobs."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import TypeAlias, cast

from btc_backtest.strategies.base import Strategy
from btc_backtest.strategies.ensemble import EnsembleComponent, WeightedEnsemble
from btc_backtest.strategies.registry import default_strategy_registry
from btc_backtest.strategies.target_weight import TargetWeightStrategy

from qt.research.datasets import DatasetCatalog
from qt.research.strategies import RuleRecipeStrategy

JsonDict: TypeAlias = dict[str, object]

_MODES = {"template", "custom_rules", "ensemble"}
_VALIDATION_PROFILES = {"quick", "standard"}


def normalize_job_request(
    payload: Mapping[str, object],
    datasets: DatasetCatalog,
) -> JsonDict:
    dataset_id = _text(payload.get("dataset_id"), "dataset_id")
    dataset = datasets.get(dataset_id)
    if dataset.get("status") != "ready":
        raise ValueError(f"dataset is not ready: {dataset_id}")
    mode = _text(payload.get("mode", "template"), "mode")
    if mode not in _MODES:
        raise ValueError(f"unsupported mode: {mode}")
    profile = _text(
        payload.get("validation_profile", "standard"),
        "validation_profile",
    )
    if profile not in _VALIDATION_PROFILES:
        raise ValueError(f"unsupported validation_profile: {profile}")
    assumptions = _assumptions(payload.get("assumptions"))
    normalized: JsonDict = {
        "dataset_id": dataset_id,
        "ohlcv_key": dataset["key"],
        "mode": mode,
        "validation_profile": profile,
        "assumptions": assumptions,
        "seed": _integer(payload.get("seed"), 7, "seed"),
    }
    registry = default_strategy_registry()
    if mode == "template":
        if payload.get("rules"):
            raise ValueError(
                "template mode cannot contain rules; use custom_rules and "
                "the custom_rule_recipe identity"
            )
        template = _mapping(payload.get("template"), "template")
        strategy_id = _text(template.get("strategy_id"), "template.strategy_id")
        parameters = _object_mapping(template.get("parameters"))
        registry.create(strategy_id, parameters)
        normalized.update(
            {
                "strategy_id": strategy_id,
                "strategy_params": parameters,
            }
        )
        return normalized
    if mode == "custom_rules":
        rules = _validate_rules(payload.get("rules"))
        normalized.update(
            {
                "strategy_id": "custom_rule_recipe",
                "strategy_params": {},
                "rules": rules,
            }
        )
        return normalized

    ensemble = _mapping(payload.get("ensemble"), "ensemble")
    raw_components = ensemble.get("components")
    if not isinstance(raw_components, list) or not 2 <= len(raw_components) <= 3:
        raise ValueError("ensemble requires two or three components")
    components: list[JsonDict] = []
    for index, raw in enumerate(raw_components, start=1):
        component = _mapping(raw, f"ensemble component {index}")
        strategy_id = _text(
            component.get("strategy_id"),
            f"ensemble component {index}.strategy_id",
        )
        parameters = _object_mapping(component.get("parameters"))
        strategy = registry.create(strategy_id, parameters)
        if not isinstance(strategy, TargetWeightStrategy):
            raise ValueError(f"{strategy_id} is not ensemble-compatible")
        weight = _decimal(
            component.get("weight", 1),
            f"ensemble component {index}.weight",
        )
        if weight <= 0:
            raise ValueError("ensemble component weight must be positive")
        components.append(
            {
                "strategy_id": strategy_id,
                "parameters": parameters,
                "weight": str(weight),
            }
        )
    normalized.update(
        {
            "strategy_id": "weighted_ensemble",
            "strategy_params": {},
            "ensemble": {"components": components},
        }
    )
    return normalized


def build_strategy(spec: Mapping[str, object]) -> Strategy:
    mode = str(spec.get("mode", "template"))
    if mode == "custom_rules":
        rules = _mapping(spec.get("rules"), "rules")
        return RuleRecipeStrategy(rules)
    registry = default_strategy_registry()
    if mode == "template":
        strategy_id = str(spec.get("strategy_id", ""))
        return registry.create(
            strategy_id,
            _object_mapping(spec.get("strategy_params")),
        )
    if mode != "ensemble":
        raise ValueError(f"unsupported mode: {mode}")
    ensemble = _mapping(spec.get("ensemble"), "ensemble")
    raw_components = ensemble.get("components")
    if not isinstance(raw_components, list):
        raise ValueError("ensemble components must be a list")
    components: list[EnsembleComponent] = []
    for raw in raw_components:
        component = _mapping(raw, "ensemble component")
        strategy = registry.create(
            str(component.get("strategy_id", "")),
            _object_mapping(component.get("parameters")),
        )
        if not isinstance(strategy, TargetWeightStrategy):
            raise ValueError(
                f"{strategy.metadata.id} is not ensemble-compatible"
            )
        components.append(
            EnsembleComponent(
                strategy=strategy,
                weight=_decimal(component.get("weight"), "weight"),
            )
        )
    return WeightedEnsemble(tuple(components))


def _assumptions(raw: object) -> JsonDict:
    values = _object_mapping(raw)
    initial_cash = _decimal(values.get("initial_cash", 10_000), "initial_cash")
    fee_bps = _decimal(values.get("fee_bps", 10), "fee_bps")
    slippage_bps = _decimal(values.get("slippage_bps", 5), "slippage_bps")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
    if fee_bps < 0 or slippage_bps < 0:
        raise ValueError("fee_bps and slippage_bps must be non-negative")
    return {
        "initial_cash": float(initial_cash),
        "fee_bps": float(fee_bps),
        "slippage_bps": float(slippage_bps),
    }


def _validate_rules(raw: object) -> JsonDict:
    rules = _mapping(raw, "rules")
    normalized: JsonDict = {}
    for group_name in ("entry", "exit"):
        group = _mapping(rules.get(group_name), f"rules.{group_name}")
        operator = str(group.get("operator", "ALL")).upper()
        if operator not in {"ALL", "ANY"}:
            raise ValueError(f"{group_name} operator must be ALL or ANY")
        conditions = group.get("conditions")
        if not isinstance(conditions, list) or not 1 <= len(conditions) <= 3:
            raise ValueError(
                f"{group_name} rules require one to three conditions"
            )
        normalized[group_name] = {
            "operator": operator,
            "conditions": [
                dict(_mapping(condition, f"{group_name} condition"))
                for condition in conditions
            ],
        }
    return normalized


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def _object_mapping(value: object) -> JsonDict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("parameters must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError("parameters require string keys")
    return {str(key): item for key, item in value.items()}


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _integer(value: object, default: int, field: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an integer") from error
