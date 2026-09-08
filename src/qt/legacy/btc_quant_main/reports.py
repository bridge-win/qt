# Migrated verbatim from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_main.backtest_archive import read_backtest_payload


@dataclass(frozen=True)
class ProductionGate:
    min_trades: int = 50
    min_profit_total_pct: float = 0.0
    min_cagr_pct: float = 0.0
    min_alpha_vs_market_pct: float = 0.0
    min_sharpe: float = 0.5
    min_profit_factor: float = 1.2
    max_drawdown_pct: float = 15.0


def summarize_backtest_zip(
    zip_path: Path,
    *,
    strategy: str,
    gate: ProductionGate = ProductionGate(),
) -> dict[str, Any]:
    result = _read_strategy_result(zip_path, strategy=strategy)
    summary = {
        "backtest_end": result["backtest_end"],
        "backtest_start": result["backtest_start"],
        "cagr_pct": _ratio_to_pct(result["cagr"]),
        "expectancy": result["expectancy"],
        "max_drawdown_abs": result["max_drawdown_abs"],
        "max_drawdown_pct": _ratio_to_pct(result["max_drawdown_account"]),
        "pairlist": result["pairlist"],
        "profit_factor": result["profit_factor"],
        "profit_total_abs": result["profit_total_abs"],
        "profit_total_pct": _ratio_to_pct(result["profit_total"]),
        "sharpe": result["sharpe"],
        "sortino": result["sortino"],
        "strategy": strategy,
        "timeframe": result["timeframe"],
        "total_trades": result["total_trades"],
        "trading_mode": result["trading_mode"],
    }
    if "market_change" in result:
        summary["market_change_pct"] = _ratio_to_pct(result["market_change"])
        summary["alpha_vs_market_pct"] = summary["profit_total_pct"] - summary["market_change_pct"]

    summary["production_gate"] = _evaluate_gate(summary, gate)
    return summary


def write_backtest_summary(*, run_dir: Path, backtest_dir: Path, strategy: str) -> Path:
    last_result_path = backtest_dir / ".last_result.json"
    last_result = json.loads(last_result_path.read_text())
    zip_path = backtest_dir / last_result["latest_backtest"]
    summary = summarize_backtest_zip(zip_path, strategy=strategy)

    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary_path


def _read_strategy_result(zip_path: Path, *, strategy: str) -> dict[str, Any]:
    payload = read_backtest_payload(zip_path)
    strategies = payload.get("strategy")
    if not isinstance(strategies, dict) or strategy not in strategies:
        raise ValueError(f"strategy {strategy} not found in {zip_path}")
    result = strategies[strategy]
    if not isinstance(result, dict):
        raise ValueError(f"strategy {strategy} result is malformed in {zip_path}")
    return result


def _evaluate_gate(summary: dict[str, Any], gate: ProductionGate) -> dict[str, Any]:
    failed_reasons = []

    if summary["total_trades"] < gate.min_trades:
        failed_reasons.append(f"total_trades below {gate.min_trades}")
    if summary["profit_total_pct"] < gate.min_profit_total_pct:
        failed_reasons.append(f"profit_total_pct below {gate.min_profit_total_pct}")
    if summary["cagr_pct"] < gate.min_cagr_pct:
        failed_reasons.append(f"cagr_pct below {gate.min_cagr_pct}")
    alpha_vs_market_pct = summary.get("alpha_vs_market_pct")
    if (
        isinstance(alpha_vs_market_pct, (int, float))
        and alpha_vs_market_pct < gate.min_alpha_vs_market_pct
    ):
        failed_reasons.append(f"alpha_vs_market_pct below {gate.min_alpha_vs_market_pct}")
    if summary["sharpe"] < gate.min_sharpe:
        failed_reasons.append(f"sharpe below {gate.min_sharpe}")
    if summary["profit_factor"] < gate.min_profit_factor:
        failed_reasons.append(f"profit_factor below {gate.min_profit_factor}")
    if summary["max_drawdown_pct"] > gate.max_drawdown_pct:
        failed_reasons.append(f"max_drawdown_pct above {gate.max_drawdown_pct:g}")

    return {"failed_reasons": failed_reasons, "passed": not failed_reasons}


def _ratio_to_pct(value: float) -> float:
    return value * 100

