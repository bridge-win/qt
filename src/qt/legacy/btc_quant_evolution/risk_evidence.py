# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.runtime_evidence import file_artifact, load_sota_runtime_binding


ENV_RISK_KEY_TYPES = {
    "FREQTRADE__TRADING_MODE": "string",
    "FREQTRADE__MAX_OPEN_TRADES": "int",
    "FREQTRADE__TRADABLE_BALANCE_RATIO": "float",
    "FREQTRADE__ORDER_TYPES__STOPLOSS": "string",
    "FREQTRADE__ORDER_TYPES__STOPLOSS_ON_EXCHANGE": "bool",
    "FREQTRADE__ORDER_TYPES__EMERGENCY_EXIT": "string",
}


@dataclass(frozen=True)
class RiskPolicy:
    trading_mode: str = "spot"
    max_open_trades: int = 1
    max_tradable_balance_ratio: float = 0.99
    stoploss_order_type: str = "limit"
    emergency_exit_order_type: str = "market"
    require_stoploss_on_exchange: bool = True
    max_risk_per_trade: float = 0.01
    max_strategy_drawdown: float = 0.10
    require_custom_stoploss: bool = True
    require_no_short: bool = True


def evaluate_config_risk(config: dict[str, Any], policy: RiskPolicy | None = None) -> dict[str, Any]:
    active_policy = policy or RiskPolicy()
    order_types = config.get("order_types")
    order_types_payload = order_types if isinstance(order_types, dict) else {}

    risk = {
        "emergency_exit": order_types_payload.get("emergency_exit"),
        "max_open_trades": config.get("max_open_trades"),
        "stake_amount": config.get("stake_amount"),
        "stoploss": order_types_payload.get("stoploss"),
        "stoploss_on_exchange": order_types_payload.get("stoploss_on_exchange"),
        "tradable_balance_ratio": config.get("tradable_balance_ratio"),
        "trading_mode": config.get("trading_mode"),
    }

    failed_reasons: list[str] = []
    if risk["trading_mode"] != active_policy.trading_mode:
        failed_reasons.append(f"trading_mode is not {active_policy.trading_mode}")

    max_open_trades = _number_or_none(risk["max_open_trades"])
    if max_open_trades is None or max_open_trades > active_policy.max_open_trades:
        failed_reasons.append(f"max_open_trades above {active_policy.max_open_trades}")

    tradable_balance_ratio = _number_or_none(risk["tradable_balance_ratio"])
    if (
        tradable_balance_ratio is None
        or tradable_balance_ratio > active_policy.max_tradable_balance_ratio
    ):
        failed_reasons.append(
            f"tradable_balance_ratio above {active_policy.max_tradable_balance_ratio:g}"
        )

    if risk["stoploss"] != active_policy.stoploss_order_type:
        failed_reasons.append(f"stoploss is not {active_policy.stoploss_order_type}")

    if active_policy.require_stoploss_on_exchange and risk["stoploss_on_exchange"] is not True:
        failed_reasons.append("stoploss_on_exchange is not true")

    if risk["emergency_exit"] != active_policy.emergency_exit_order_type:
        failed_reasons.append(f"emergency_exit is not {active_policy.emergency_exit_order_type}")

    return {
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
        "policy": asdict(active_policy),
        "risk": risk,
    }


def evaluate_env_risk(env_values: dict[str, str], policy: RiskPolicy | None = None) -> dict[str, Any]:
    active_policy = policy or RiskPolicy()
    risk_overrides: dict[str, object] = {}
    ignored_freqtrade_key_count = 0

    for key, value in sorted(env_values.items()):
        if key not in ENV_RISK_KEY_TYPES:
            if key.startswith("FREQTRADE__"):
                ignored_freqtrade_key_count += 1
            continue
        risk_overrides[key] = _parse_env_value(value, ENV_RISK_KEY_TYPES[key])

    failed_reasons: list[str] = []
    if risk_overrides.get("FREQTRADE__TRADING_MODE") not in (None, active_policy.trading_mode):
        failed_reasons.append(f"env override FREQTRADE__TRADING_MODE is not {active_policy.trading_mode}")

    max_open_trades = risk_overrides.get("FREQTRADE__MAX_OPEN_TRADES")
    if max_open_trades is not None:
        numeric_value = _number_or_none(max_open_trades)
        if numeric_value is None or numeric_value > active_policy.max_open_trades:
            failed_reasons.append(f"env override FREQTRADE__MAX_OPEN_TRADES above {active_policy.max_open_trades}")

    tradable_balance_ratio = risk_overrides.get("FREQTRADE__TRADABLE_BALANCE_RATIO")
    if tradable_balance_ratio is not None:
        numeric_value = _number_or_none(tradable_balance_ratio)
        if numeric_value is None or numeric_value > active_policy.max_tradable_balance_ratio:
            failed_reasons.append(
                "env override FREQTRADE__TRADABLE_BALANCE_RATIO "
                f"above {active_policy.max_tradable_balance_ratio:g}"
            )

    if risk_overrides.get("FREQTRADE__ORDER_TYPES__STOPLOSS") not in (
        None,
        active_policy.stoploss_order_type,
    ):
        failed_reasons.append(
            f"env override FREQTRADE__ORDER_TYPES__STOPLOSS is not {active_policy.stoploss_order_type}"
        )

    if (
        active_policy.require_stoploss_on_exchange
        and risk_overrides.get("FREQTRADE__ORDER_TYPES__STOPLOSS_ON_EXCHANGE") is False
    ):
        failed_reasons.append("env override FREQTRADE__ORDER_TYPES__STOPLOSS_ON_EXCHANGE is not true")

    if risk_overrides.get("FREQTRADE__ORDER_TYPES__EMERGENCY_EXIT") not in (
        None,
        active_policy.emergency_exit_order_type,
    ):
        failed_reasons.append(
            "env override FREQTRADE__ORDER_TYPES__EMERGENCY_EXIT "
            f"is not {active_policy.emergency_exit_order_type}"
        )

    return {
        "env_risk": {
            "ignored_freqtrade_key_count": ignored_freqtrade_key_count,
            "risk_overrides": risk_overrides,
        },
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
        "policy": asdict(active_policy),
    }


def evaluate_strategy_risk(strategy: object, policy: RiskPolicy | None = None) -> dict[str, Any]:
    active_policy = policy or RiskPolicy()
    protections = _protection_payload(getattr(strategy, "protections", None))
    max_drawdown = _max_drawdown_protection(protections)
    risk_per_trade = getattr(strategy, "risk_per_trade", None)

    strategy_risk = {
        "can_short": getattr(strategy, "can_short", None),
        "max_allowed_drawdown": _number_or_none(max_drawdown.get("max_allowed_drawdown")),
        "max_drawdown_protection": bool(max_drawdown),
        "risk_per_trade_default": _number_or_none(_parameter_default(risk_per_trade)),
        "risk_per_trade_max": _number_or_none(_parameter_high(risk_per_trade)),
        "stoploss": _number_or_none(getattr(strategy, "stoploss", None)),
        "use_custom_stoploss": getattr(strategy, "use_custom_stoploss", None),
    }
    failed_reasons = _strategy_failed_reasons(strategy_risk, active_policy)

    return {
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
        "policy": asdict(active_policy),
        "strategy_risk": strategy_risk,
    }


def evaluate_strategy_source_risk(
    strategy_path: Path,
    strategy_name: str,
    policy: RiskPolicy | None = None,
) -> dict[str, Any]:
    active_policy = policy or RiskPolicy()
    if not strategy_path.exists():
        return {
            "failed_reasons": ["missing strategy source"],
            "passed": False,
            "policy": asdict(active_policy),
            "strategy_risk": {},
        }

    module = ast.parse(strategy_path.read_text())
    strategy_class = _find_class(module, strategy_name)
    if strategy_class is None:
        return {
            "failed_reasons": [f"strategy {strategy_name} not found"],
            "passed": False,
            "policy": asdict(active_policy),
            "strategy_risk": {},
        }

    attributes = _class_attributes(strategy_class)
    protections = _source_protections(strategy_class)
    max_drawdown = _max_drawdown_protection(protections)
    risk_per_trade = attributes.get("risk_per_trade")
    constants = _source_constants(strategy_path, strategy_name)

    strategy_risk = {
        "can_short": attributes.get("can_short"),
        "max_allowed_drawdown": _number_or_none(max_drawdown.get("max_allowed_drawdown")),
        "max_drawdown_protection": bool(max_drawdown),
        "risk_per_trade_default": _number_or_none(
            _source_parameter_default(risk_per_trade, constants=constants)
        ),
        "risk_per_trade_max": _number_or_none(
            _source_parameter_high(risk_per_trade, constants=constants)
        ),
        "stoploss": _number_or_none(attributes.get("stoploss")),
        "use_custom_stoploss": attributes.get("use_custom_stoploss"),
    }
    failed_reasons = _strategy_failed_reasons(strategy_risk, active_policy)
    return {
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
        "policy": asdict(active_policy),
        "strategy_risk": strategy_risk,
    }


def write_risk_evidence(
    *,
    config_path: Path,
    output_dir: Path,
    root_dir: Path,
    policy: RiskPolicy | None = None,
    strategy_name: str | None = None,
    strategy_path: Path | None = None,
    env_file_path: Path | None = None,
    candidate_package_path: Path | None = None,
) -> Path:
    config_payload = evaluate_config_risk(
        json.loads(config_path.read_text()),
        policy=policy,
    )
    failed_reasons = list(config_payload["failed_reasons"])
    artifacts = {
        "config": _file_artifact(root_dir=root_dir, path=config_path),
    }
    checks = {
        "config": _check_from_payload(config_payload),
        "env": {"passed": None, "reason": None},
        "strategy": {"passed": None, "reason": None},
    }

    payload = {
        "checks": checks,
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
        "policy": config_payload["policy"],
        "risk": config_payload["risk"],
    }

    if candidate_package_path is not None:
        runtime_selection, runtime_sources = load_sota_runtime_binding(
            root_dir=root_dir,
            candidate_package_path=candidate_package_path,
        )
        if strategy_name != runtime_selection["strategy"]:
            raise ValueError("strategy does not match candidate package")
        if strategy_path is None:
            raise ValueError("strategy source is required for candidate package evidence")
        strategy_artifact = file_artifact(root_dir=root_dir, path=strategy_path)
        if strategy_artifact != runtime_sources["sota"]:
            raise ValueError("strategy source does not match candidate package")
        payload["runtime_selection"] = runtime_selection
        payload["strategy_name"] = strategy_name
        artifacts["strategy_runtime_sources"] = runtime_sources

    if strategy_path is not None and strategy_name is not None:
        strategy_payload = evaluate_strategy_source_risk(
            strategy_path=strategy_path,
            strategy_name=strategy_name,
            policy=policy,
        )
        payload["strategy_risk"] = strategy_payload["strategy_risk"]
        failed_reasons.extend(strategy_payload["failed_reasons"])
        checks["strategy"] = _check_from_payload(strategy_payload)
        artifacts["strategy"] = _file_artifact(root_dir=root_dir, path=strategy_path)
        payload["failed_reasons"] = failed_reasons
        payload["passed"] = not failed_reasons

    if env_file_path is not None:
        if env_file_path.exists():
            env_payload = evaluate_env_risk(_read_env_file(env_file_path), policy=policy)
        else:
            env_payload = {
                "env_risk": {
                    "ignored_freqtrade_key_count": 0,
                    "risk_overrides": {},
                },
                "failed_reasons": ["missing env file"],
                "passed": False,
                "policy": payload["policy"],
            }
        payload["env_risk"] = env_payload["env_risk"]
        failed_reasons.extend(env_payload["failed_reasons"])
        checks["env"] = _check_from_payload(env_payload)
        artifacts["env_file"] = _sensitive_file_artifact(root_dir=root_dir, path=env_file_path)
        payload["failed_reasons"] = failed_reasons
        payload["passed"] = not failed_reasons

    payload["artifacts"] = artifacts

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "risk.json"
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Write production risk-control evidence.")
    parser.add_argument("--config", type=Path, default=Path("user_data/config.json"))
    parser.add_argument("--strategy-name", default="BtcDonchianAtr")
    parser.add_argument("--strategy-source", type=Path, default=Path("user_data/strategies/BtcDonchianAtr.py"))
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--candidate-package", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/live-evidence"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    parser.add_argument("--max-open-trades", type=int, default=RiskPolicy.max_open_trades)
    parser.add_argument(
        "--max-tradable-balance-ratio",
        type=float,
        default=RiskPolicy.max_tradable_balance_ratio,
    )
    args = parser.parse_args()

    policy = RiskPolicy(
        max_open_trades=args.max_open_trades,
        max_tradable_balance_ratio=args.max_tradable_balance_ratio,
    )
    report_path = write_risk_evidence(
        config_path=args.config,
        output_dir=args.output_dir,
        root_dir=args.root_dir.resolve(),
        policy=policy,
        strategy_name=args.strategy_name,
        strategy_path=args.strategy_source,
        env_file_path=args.env_file,
        candidate_package_path=args.candidate_package,
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] is True else 1


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and math.isfinite(value):
        return float(value)
    return None


def _parse_env_value(value: str, value_type: str) -> object:
    if value_type == "bool":
        return value.strip().lower() == "true"
    if value_type == "int":
        try:
            return int(value)
        except ValueError:
            return value
    if value_type == "float":
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _strip_env_quotes(value.strip())
    return values


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _strategy_failed_reasons(strategy_risk: dict[str, Any], policy: RiskPolicy) -> list[str]:
    failed_reasons: list[str] = []
    if policy.require_no_short and strategy_risk["can_short"] is not False:
        failed_reasons.append("strategy can_short is not false")

    if policy.require_custom_stoploss and strategy_risk["use_custom_stoploss"] is not True:
        failed_reasons.append("strategy use_custom_stoploss is not true")

    risk_per_trade_max = _number_or_none(strategy_risk["risk_per_trade_max"])
    if risk_per_trade_max is None or risk_per_trade_max > policy.max_risk_per_trade:
        failed_reasons.append(f"strategy risk_per_trade max above {policy.max_risk_per_trade:g}")

    risk_per_trade_default = _number_or_none(strategy_risk["risk_per_trade_default"])
    if risk_per_trade_default is None or risk_per_trade_default > policy.max_risk_per_trade:
        failed_reasons.append(f"strategy risk_per_trade default above {policy.max_risk_per_trade:g}")

    if strategy_risk["max_drawdown_protection"] is not True:
        failed_reasons.append("strategy MaxDrawdown protection missing")
    else:
        max_allowed_drawdown = _number_or_none(strategy_risk["max_allowed_drawdown"])
        if max_allowed_drawdown is None or max_allowed_drawdown > policy.max_strategy_drawdown:
            failed_reasons.append(f"strategy MaxDrawdown above {policy.max_strategy_drawdown:g}")

    return failed_reasons


def _check_from_payload(payload: dict[str, Any]) -> dict[str, bool | str | None]:
    if payload["passed"] is True:
        return {"passed": True, "reason": None}
    return {"passed": False, "reason": "; ".join(payload["failed_reasons"])}


def _parameter_default(parameter: object) -> object:
    value = getattr(parameter, "value", None)
    if value is not None:
        return value
    return getattr(parameter, "default", None)


def _parameter_high(parameter: object) -> object:
    return getattr(parameter, "high", None)


def _protection_payload(protections: object) -> list[dict[str, Any]]:
    if not isinstance(protections, list):
        return []
    return [protection for protection in protections if isinstance(protection, dict)]


def _max_drawdown_protection(protections: list[dict[str, Any]]) -> dict[str, Any]:
    for protection in protections:
        if protection.get("method") == "MaxDrawdown":
            return protection
    return {}


def _find_class(module: ast.Module, class_name: str) -> ast.ClassDef | None:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _class_attributes(strategy_class: ast.ClassDef) -> dict[str, object]:
    attributes: dict[str, object] = {}
    for node in strategy_class.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            attributes[node.targets[0].id] = _literal_or_node(node.value)
    return attributes


def _source_protections(strategy_class: ast.ClassDef) -> list[dict[str, Any]]:
    for node in strategy_class.body:
        if isinstance(node, ast.FunctionDef) and node.name == "protections":
            for statement in node.body:
                if isinstance(statement, ast.Return):
                    value = _literal_or_node(statement.value)
                    return _protection_payload(value)
    return []


def _source_parameter_default(
    value: object,
    *,
    constants: dict[str, object] | None = None,
) -> object:
    if isinstance(value, ast.Call):
        for keyword in value.keywords:
            if keyword.arg == "default":
                return _resolve_source_value(keyword.value, constants or {})
    return _parameter_default(value)


def _source_parameter_high(
    value: object,
    *,
    constants: dict[str, object] | None = None,
) -> object:
    if isinstance(value, ast.Call):
        if len(value.args) >= 2:
            return _resolve_source_value(value.args[1], constants or {})
        for keyword in value.keywords:
            if keyword.arg == "high":
                return _resolve_source_value(keyword.value, constants or {})
    return _parameter_high(value)


def _source_constants(strategy_path: Path, strategy_name: str) -> dict[str, object]:
    if strategy_name != "Sota":
        return {}
    params_path = strategy_path.with_name("sota_params.py")
    if not params_path.is_file():
        return {}
    module = ast.parse(params_path.read_text())
    constants: dict[str, object] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            constants[target.id] = _literal_or_node(node.value)
    return constants


def _resolve_source_value(node: ast.AST, constants: dict[str, object]) -> object:
    literal = _literal_or_node(node)
    if not isinstance(literal, ast.AST):
        return literal
    if isinstance(node, ast.Name):
        return constants.get(node.id, node)
    if isinstance(node, ast.Subscript):
        container = _resolve_source_value(node.value, constants)
        key = _resolve_source_value(node.slice, constants)
        if isinstance(container, dict) and key in container:
            return container[key]
    return node


def _literal_or_node(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except ValueError:
        return node


def _file_artifact(*, root_dir: Path, path: Path) -> dict[str, Any]:
    try:
        relative_path = path.relative_to(root_dir)
    except ValueError:
        relative_path = path
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


def _sensitive_file_artifact(*, root_dir: Path, path: Path) -> dict[str, Any]:
    try:
        relative_path = path.relative_to(root_dir)
    except ValueError:
        relative_path = path
    exists = path.is_file()
    return {
        "exists": exists,
        "path": relative_path.as_posix(),
        "sensitive": True,
        "sha256": _sha256(path) if exists else None,
        "size_bytes": path.stat().st_size if exists else None,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

