# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.backtest_archive import read_backtest_payload


@dataclass(frozen=True)
class RobustnessPolicy:
    min_trades: int = 50
    max_loss_cluster_drawdown_pct: float = 25.0
    min_cost_stress_profit_pct: float = 0.0
    fee_bps_per_side: float = 0.0
    slippage_bps: float = 10.0

    def __post_init__(self) -> None:
        if self.min_trades < 0:
            raise ValueError("min_trades cannot be negative")
        if self.max_loss_cluster_drawdown_pct < 0:
            raise ValueError("max_loss_cluster_drawdown_pct cannot be negative")
        if self.fee_bps_per_side < 0:
            raise ValueError("fee_bps_per_side cannot be negative")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")


def evaluate_trade_robustness(
    *,
    trades: list[dict[str, Any]],
    policy: RobustnessPolicy = RobustnessPolicy(),
) -> dict[str, Any]:
    returns = [float(trade["profit_ratio"]) for trade in trades]
    finite_returns = all(math.isfinite(value) for value in returns)
    round_trip_cost_bps = 2 * (policy.fee_bps_per_side + policy.slippage_bps)
    cost_stressed_returns = [
        profit_ratio - (round_trip_cost_bps / 10_000)
        for profit_ratio in returns
    ] if finite_returns else []
    chronological_max_drawdown_pct = _max_drawdown_pct(returns) if returns and finite_returns else None
    loss_cluster_returns = sorted(returns) if finite_returns else []
    loss_cluster_max_drawdown_pct = _max_drawdown_pct(loss_cluster_returns) if returns and finite_returns else None
    compounded_trade_profit_pct = _compounded_trade_profit_pct(returns) if returns and finite_returns else None
    loss_cluster_compounded_trade_profit_pct = (
        _compounded_trade_profit_pct(loss_cluster_returns) if returns and finite_returns else None
    )
    cost_stress_compounded_trade_profit_pct = (
        _compounded_trade_profit_pct(cost_stressed_returns) if returns and finite_returns else None
    )

    result = {
        "chronological_max_drawdown_pct": chronological_max_drawdown_pct,
        "compounded_trade_profit_pct": compounded_trade_profit_pct,
        "cost_stress": {
            "compounded_trade_profit_pct": cost_stress_compounded_trade_profit_pct,
            "fee_bps_per_side": policy.fee_bps_per_side,
            "round_trip_cost_bps": round_trip_cost_bps,
            "slippage_bps_per_side": policy.slippage_bps,
        },
        "loss_cluster": {
            "compounded_trade_profit_pct": loss_cluster_compounded_trade_profit_pct,
            "max_drawdown_pct": loss_cluster_max_drawdown_pct,
        },
        "trade_count": len(returns),
    }
    result["robustness_gate"] = _evaluate_gate(result, policy, finite_returns=finite_returns)
    return result


def write_robustness_report(
    *,
    run_dir: Path,
    backtest_dir: Path,
    strategy: str,
    policy: RobustnessPolicy = RobustnessPolicy(),
) -> Path:
    last_result = json.loads((backtest_dir / ".last_result.json").read_text())
    zip_path = backtest_dir / last_result["latest_backtest"]
    trades = _read_strategy_trades(zip_path, strategy=strategy)
    report = evaluate_trade_robustness(trades=trades, policy=policy)

    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "robustness.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report_path


def _read_strategy_trades(zip_path: Path, *, strategy: str) -> list[dict[str, Any]]:
    payload = read_backtest_payload(zip_path)
    strategies = payload.get("strategy")
    if not isinstance(strategies, dict) or strategy not in strategies:
        raise ValueError(f"strategy {strategy} not found in {zip_path}")
    strategy_result = strategies[strategy]
    if not isinstance(strategy_result, dict):
        raise ValueError(f"strategy {strategy} result is malformed in {zip_path}")
    trades = strategy_result.get("trades")
    if not isinstance(trades, list):
        return []
    return trades


def _evaluate_gate(
    result: dict[str, Any],
    policy: RobustnessPolicy,
    *,
    finite_returns: bool,
) -> dict[str, Any]:
    failed_reasons = []

    if not finite_returns:
        failed_reasons.append("non-finite profit_ratio")

    if result["trade_count"] == 0:
        failed_reasons.append("no trades found")
    if result["trade_count"] < policy.min_trades:
        failed_reasons.append(f"total_trades below {policy.min_trades}")

    loss_cluster_max_drawdown_pct = result["loss_cluster"]["max_drawdown_pct"]
    if (
        _is_finite_number(loss_cluster_max_drawdown_pct)
        and loss_cluster_max_drawdown_pct > policy.max_loss_cluster_drawdown_pct
    ):
        failed_reasons.append(
            f"loss_cluster_max_drawdown_pct above {policy.max_loss_cluster_drawdown_pct:g}"
        )

    cost_stress_compounded_trade_profit_pct = result["cost_stress"]["compounded_trade_profit_pct"]
    if (
        _is_finite_number(cost_stress_compounded_trade_profit_pct)
        and cost_stress_compounded_trade_profit_pct < policy.min_cost_stress_profit_pct
    ):
        failed_reasons.append(
            f"cost_stress_compounded_trade_profit_pct below {policy.min_cost_stress_profit_pct}"
        )

    return {"failed_reasons": failed_reasons, "passed": not failed_reasons}


def _compounded_trade_profit_pct(returns: list[float]) -> float:
    equity = 1.0
    for profit_ratio in returns:
        equity *= 1 + profit_ratio
    return _round_pct((equity - 1) * 100)


def _max_drawdown_pct(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for profit_ratio in returns:
        equity *= 1 + profit_ratio
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak
        max_drawdown = max(max_drawdown, drawdown)
    return _round_pct(max_drawdown * 100)


def _round_pct(value: float) -> float:
    return round(value, 10)


def _is_finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)

