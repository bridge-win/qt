# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from qt.legacy.btc_quant_evolution.backtest_archive import read_backtest_payload
from qt.legacy.btc_quant_evolution.ohlcv_integrity import data_file_path


@dataclass(frozen=True)
class RegimePolicy:
    trend_window_candles: int = 30
    volatility_window_candles: int = 30
    trend_threshold_pct: float = 5.0
    high_volatility_threshold_pct: float = 10.0
    min_total_trades: int = 50
    min_trend_regimes: int = 2
    min_volatility_regimes: int = 2
    min_regime_profit_pct: float = -10.0

    def __post_init__(self) -> None:
        if self.trend_window_candles <= 0:
            raise ValueError("trend_window_candles must be positive")
        if self.volatility_window_candles <= 0:
            raise ValueError("volatility_window_candles must be positive")
        if self.trend_threshold_pct < 0:
            raise ValueError("trend_threshold_pct cannot be negative")
        if self.high_volatility_threshold_pct < 0:
            raise ValueError("high_volatility_threshold_pct cannot be negative")
        if self.min_total_trades < 0:
            raise ValueError("min_total_trades cannot be negative")
        if self.min_trend_regimes < 0:
            raise ValueError("min_trend_regimes cannot be negative")
        if self.min_volatility_regimes < 0:
            raise ValueError("min_volatility_regimes cannot be negative")


def evaluate_regime_robustness(
    *,
    candles: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    policy: RegimePolicy = RegimePolicy(),
) -> dict[str, Any]:
    candle_states = _build_candle_states(candles=candles, policy=policy)
    classified_trades = _classify_trades(trades=trades, candle_states=candle_states)
    trend_regimes = _aggregate_regimes(
        classified_trades=classified_trades,
        regime_key="trend_regime",
        labels=("bear", "bull", "sideways"),
    )
    volatility_regimes = _aggregate_regimes(
        classified_trades=classified_trades,
        regime_key="volatility_regime",
        labels=("high", "low"),
    )
    result = {
        "classified_trade_count": len(classified_trades),
        "policy": asdict(policy),
        "trade_count": len(trades),
        "trend_regimes": trend_regimes,
        "volatility_regimes": volatility_regimes,
    }
    result["regime_gate"] = _evaluate_gate(result=result, policy=policy)
    return result


def write_regime_report(
    *,
    run_dir: Path,
    root_dir: Path,
    backtest_dir: Path,
    exchange: str,
    pair: str,
    timeframe: str,
    strategy: str,
    policy: RegimePolicy = RegimePolicy(),
) -> Path:
    ohlcv_path = data_file_path(root_dir=root_dir, exchange=exchange, pair=pair, timeframe=timeframe)
    last_result = json.loads((backtest_dir / ".last_result.json").read_text())
    zip_path = backtest_dir / last_result["latest_backtest"]
    trades = _read_strategy_trades(zip_path, strategy=strategy)
    candles = _read_ohlcv_candles(ohlcv_path)
    report = evaluate_regime_robustness(candles=candles, trades=trades, policy=policy)
    report["ohlcv_path"] = _relative_path(path=ohlcv_path, root_dir=root_dir)

    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "regime.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report_path


def _build_candle_states(*, candles: list[dict[str, Any]], policy: RegimePolicy) -> list[dict[str, Any]]:
    ordered = sorted(
        ({"date": _parse_timestamp(candle["date"]), "close": float(candle["close"])} for candle in candles),
        key=lambda candle: candle["date"],
    )
    states = []
    for index, candle in enumerate(ordered):
        trend_return_pct = _window_return_pct(ordered=ordered, index=index, window=policy.trend_window_candles)
        realized_volatility_pct = _realized_volatility_pct(
            ordered=ordered,
            index=index,
            window=policy.volatility_window_candles,
        )
        states.append(
            {
                "date": candle["date"],
                "trend_regime": _trend_regime(
                    trend_return_pct=trend_return_pct,
                    threshold_pct=policy.trend_threshold_pct,
                ),
                "volatility_regime": (
                    "high" if realized_volatility_pct >= policy.high_volatility_threshold_pct else "low"
                ),
            }
        )
    return states


def _classify_trades(
    *,
    trades: list[dict[str, Any]],
    candle_states: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    state_dates = [state["date"] for state in candle_states]
    classified = []
    for trade in trades:
        close_date = _trade_close_date(trade)
        state_index = bisect_right(state_dates, close_date) - 1
        if state_index < 0:
            continue
        state = candle_states[state_index]
        classified.append(
            {
                "profit_ratio": float(trade["profit_ratio"]),
                "trend_regime": state["trend_regime"],
                "volatility_regime": state["volatility_regime"],
            }
        )
    return classified


def _aggregate_regimes(
    *,
    classified_trades: list[dict[str, Any]],
    regime_key: str,
    labels: tuple[str, ...],
) -> dict[str, dict[str, float | int]]:
    report = {}
    for label in labels:
        returns = [
            float(trade["profit_ratio"])
            for trade in classified_trades
            if trade[regime_key] == label
        ]
        if not returns:
            continue
        report[label] = {
            "compounded_trade_profit_pct": _compounded_trade_profit_pct(returns),
            "trade_count": len(returns),
        }
    return report


def _evaluate_gate(*, result: dict[str, Any], policy: RegimePolicy) -> dict[str, Any]:
    failed_reasons = []
    if result["trade_count"] == 0:
        failed_reasons.append("no trades found")
    if result["trade_count"] < policy.min_total_trades:
        failed_reasons.append(f"total_trades below {policy.min_total_trades}")
    if result["classified_trade_count"] < result["trade_count"]:
        failed_reasons.append("some trades could not be matched to OHLCV regimes")

    covered_trend_regimes = _covered_regime_count(result["trend_regimes"])
    if covered_trend_regimes < policy.min_trend_regimes:
        failed_reasons.append(f"covered_trend_regimes below {policy.min_trend_regimes}")
    covered_volatility_regimes = _covered_regime_count(result["volatility_regimes"])
    if covered_volatility_regimes < policy.min_volatility_regimes:
        failed_reasons.append(f"covered_volatility_regimes below {policy.min_volatility_regimes}")

    for group_name in ("trend_regimes", "volatility_regimes"):
        for regime_name, metrics in result[group_name].items():
            profit_pct = metrics["compounded_trade_profit_pct"]
            if profit_pct < policy.min_regime_profit_pct:
                failed_reasons.append(
                    f"{group_name}.{regime_name}.compounded_trade_profit_pct below {policy.min_regime_profit_pct}"
                )
    return {"failed_reasons": failed_reasons, "passed": not failed_reasons}


def _read_strategy_trades(zip_path: Path, *, strategy: str) -> list[dict[str, Any]]:
    payload = read_backtest_payload(zip_path)
    strategies = payload.get("strategy")
    if not isinstance(strategies, dict) or strategy not in strategies:
        raise ValueError(f"strategy {strategy} not found in {zip_path}")
    strategy_result = strategies[strategy]
    if not isinstance(strategy_result, dict):
        raise ValueError(f"strategy {strategy} result is malformed in {zip_path}")
    trades = strategy_result.get("trades")
    return trades if isinstance(trades, list) else []


def _read_ohlcv_candles(path: Path) -> list[dict[str, Any]]:
    frame = pd.read_feather(path)
    return [
        {"date": row["date"], "close": row["close"]}
        for row in frame[["date", "close"]].to_dict("records")
    ]


def _window_return_pct(*, ordered: list[dict[str, Any]], index: int, window: int) -> float:
    start_index = max(0, index - window)
    start_close = float(ordered[start_index]["close"])
    if start_close == 0:
        return 0.0
    end_close = float(ordered[index]["close"])
    return ((end_close / start_close) - 1) * 100


def _realized_volatility_pct(*, ordered: list[dict[str, Any]], index: int, window: int) -> float:
    start_index = max(1, index - window + 1)
    squared_returns = []
    for current_index in range(start_index, index + 1):
        previous_close = float(ordered[current_index - 1]["close"])
        if previous_close == 0:
            continue
        current_close = float(ordered[current_index]["close"])
        squared_returns.append(((current_close / previous_close) - 1) ** 2)
    return (sum(squared_returns) ** 0.5) * 100


def _trend_regime(*, trend_return_pct: float, threshold_pct: float) -> str:
    if trend_return_pct >= threshold_pct:
        return "bull"
    if trend_return_pct <= -threshold_pct:
        return "bear"
    return "sideways"


def _covered_regime_count(regimes: dict[str, dict[str, float | int]]) -> int:
    return sum(1 for metrics in regimes.values() if metrics["trade_count"] > 0)


def _trade_close_date(trade: dict[str, Any]) -> datetime:
    for key in ("close_date", "close_timestamp", "close_datetime"):
        value = trade.get(key)
        if value is not None:
            return _parse_timestamp(value)
    raise ValueError("trade missing close date")


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    raise ValueError(f"unsupported timestamp value: {value!r}")


def _compounded_trade_profit_pct(returns: list[float]) -> float:
    equity = 1.0
    for profit_ratio in returns:
        equity *= 1 + profit_ratio
    return round((equity - 1) * 100, 10)


def _relative_path(*, path: Path, root_dir: Path) -> str:
    try:
        return path.resolve().relative_to(root_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()

