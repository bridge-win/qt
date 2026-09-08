# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import json
import math
from pathlib import Path


SOTA_PARAMETER_SPECS = {
    "atr_multiple": {"high": 4.0, "integer": False, "low": 2.5},
    "external_entry_threshold": {"high": 0.20, "integer": False, "low": -0.10},
    "min_external_confirmations": {"high": 4, "integer": True, "low": 2},
    "momentum_entry_threshold": {"high": 0.75, "integer": False, "low": 0.50},
    "risk_per_trade": {"high": 0.0075, "integer": False, "low": 0.0025},
    "target_annual_vol": {"high": 0.35, "integer": False, "low": 0.15},
}


def load_sota_candidate_parameters(path: Path, *, root_dir: Path) -> dict[str, float | int]:
    root_dir = root_dir.resolve()
    resolved = path.resolve() if path.is_absolute() else (root_dir / path).resolve()
    try:
        resolved.relative_to(root_dir)
    except ValueError as exc:
        raise ValueError("Sota candidate path is outside runtime root") from exc
    if not resolved.is_file():
        raise ValueError(f"missing Sota candidate: {resolved}")
    try:
        payload = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Sota candidate is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Sota candidate must be a JSON object")
    return validate_sota_candidate_parameters(payload)


def validate_sota_candidate_parameters(payload: dict[str, object]) -> dict[str, float | int]:
    if payload.get("strategy_name") != "Sota":
        raise ValueError("Sota candidate strategy_name must be Sota")
    version = payload.get("ft_stratparam_v")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("Sota candidate ft_stratparam_v must be 1")
    params = payload.get("params")
    buy = params.get("buy") if isinstance(params, dict) else None
    if not isinstance(buy, dict):
        raise ValueError("Sota candidate params.buy must be an object")
    if set(buy) != set(SOTA_PARAMETER_SPECS):
        raise ValueError("Sota candidate params.buy has unsupported or missing parameter names")

    validated: dict[str, float | int] = {}
    for name, spec in SOTA_PARAMETER_SPECS.items():
        value = buy[name]
        if spec["integer"]:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"Sota candidate parameter {name} must be an integer")
            numeric_value: float | int = value
        else:
            if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"Sota candidate parameter {name} must be finite numeric")
            numeric_value = float(value)
        low = spec["low"]
        high = spec["high"]
        if numeric_value < low or numeric_value > high:
            raise ValueError(
                f"Sota candidate parameter {name} is outside [{low}, {high}]"
            )
        validated[name] = numeric_value
    return validated

