"""No-code research strategies with explicit identities."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import pandas as pd
from btc_backtest.strategies.base import StrategyContext, StrategyMetadata
from btc_backtest.strategies.target_weight import TargetWeightStrategy


class RuleRecipeStrategy(TargetWeightStrategy):  # type: ignore[misc]
    metadata = StrategyMetadata(
        id="custom_rule_recipe",
        version="1.0.0",
        description="Explicit no-code long/cash indicator recipe.",
        warmup_bars=502,
        supported_timeframes=("1h", "1d"),
        requires_full_history=False,
    )

    def __init__(self, rules: Mapping[str, object]) -> None:
        super().__init__()
        self.rules = dict(rules)
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        if self._target == 0 and _group_matches(
            context.bars,
            self.rules.get("entry"),
            default=False,
        ):
            self._target = Decimal("1")
        elif self._target > 0 and _group_matches(
            context.bars,
            self.rules.get("exit"),
            default=False,
        ):
            self._target = Decimal("0")
        return self._target

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        return (
            "custom_recipe_entry"
            if target_value > current_value
            else "custom_recipe_exit"
        )


def _group_matches(
    frame: pd.DataFrame,
    raw: object,
    *,
    default: bool,
) -> bool:
    if not isinstance(raw, Mapping):
        return default
    conditions = raw.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return default
    matches = [
        _condition_matches(frame, condition)
        for condition in conditions
        if isinstance(condition, Mapping)
    ]
    if not matches:
        return default
    return any(matches) if str(raw.get("operator", "ALL")).upper() == "ANY" else all(matches)


def _condition_matches(frame: pd.DataFrame, condition: Mapping[object, object]) -> bool:
    if frame.empty or "close" not in frame:
        return False
    indicator = str(condition.get("indicator", ""))
    window = max(2, min(_integer(condition.get("window"), 20), 500))
    if len(frame) < window + 1:
        return False
    close = pd.to_numeric(frame["close"], errors="coerce")
    current = float(close.iloc[-1])
    if indicator in {"close_above_sma", "close_below_sma"}:
        average = float(close.tail(window).mean())
        return (
            current > average
            if indicator == "close_above_sma"
            else current < average
        )
    if indicator in {"rsi_below", "rsi_above"}:
        delta = close.diff().dropna().tail(window)
        gains = delta.clip(lower=0).mean()
        losses = (-delta.clip(upper=0)).mean()
        rsi = 100.0 if losses == 0 else 100 - (100 / (1 + gains / losses))
        default_threshold = 30 if indicator == "rsi_below" else 70
        threshold = float(str(condition.get("threshold", default_threshold)))
        return rsi < threshold if indicator == "rsi_below" else rsi > threshold
    band = close.tail(window)
    average = float(band.mean())
    deviation = float(band.std(ddof=0))
    if indicator == "bollinger_lower_touch":
        return current <= average - 2 * deviation
    if indicator == "bollinger_upper_touch":
        return current >= average + 2 * deviation
    if indicator == "donchian_breakout":
        high = pd.to_numeric(frame.get("high", close), errors="coerce")
        return current > float(high.iloc[-window:-1].max())
    if indicator == "atr_breakout" and {"high", "low"}.issubset(frame.columns):
        high = pd.to_numeric(frame["high"], errors="coerce")
        low = pd.to_numeric(frame["low"], errors="coerce")
        atr = float((high - low).tail(window).mean())
        return current > float(close.iloc[-2]) + atr
    return False


def _integer(value: object, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
