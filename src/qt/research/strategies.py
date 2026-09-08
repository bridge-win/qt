"""No-code research strategies with explicit identities."""

from __future__ import annotations

import json
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


class LabRuleVersionStrategy(TargetWeightStrategy):  # type: ignore[misc]
    """Execute an immutable lab rules version on completed bars only.

    This intentionally supports the typed rule AST, not arbitrary plugin
    source. Plugin execution remains a separately isolated-worker concern.
    Unsupported indicators fail the job before any result publication.
    """

    metadata = StrategyMetadata(
        id="lab_rule_version",
        version="1.0.0",
        description="Immutable research-lab rules strategy.",
        warmup_bars=200,
        supported_timeframes=("1h", "1d"),
        requires_full_history=False,
    )

    def __init__(
        self,
        content: Mapping[str, object],
        parameter_overrides: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__()
        if content.get("mode") != "rules":
            raise ValueError("only immutable lab rules versions are executable in this worker")
        self.content = _bind_lab_parameters(content, parameter_overrides or {})
        # StrategyMetadata is a frozen Pydantic model, not a dataclass.  Keep
        # the class-level identity immutable while giving this version its
        # source-derived warmup requirement.
        self.metadata = self.metadata.model_copy(update={"warmup_bars": _lab_warmup(self.content)})
        self._target = Decimal("0")
        self._last_rule_trace: dict[str, object] = {
            "phase": "warmup",
            "outcome": "unknown",
            "reason": "no completed bar has been evaluated",
        }

    def target_weight(self, context: StrategyContext) -> Decimal:
        frame = context.bars
        phase = "entry" if self._target == 0 else "exit"
        rule = self.content.get("entry_rule") if phase == "entry" else self.content.get("exit_rule")
        diagnostic = _lab_rule_diagnostic(frame, rule)
        self._last_rule_trace = {"phase": phase, **diagnostic}
        if diagnostic["outcome"] is True and phase == "entry":
            self._target = Decimal("1")
        elif diagnostic["outcome"] is True:
            self._target = Decimal("0")
        return self._target

    def rebalance_reason(self, *, current_value: Decimal, target_value: Decimal) -> str:
        return "lab_rule_entry" if target_value > current_value else "lab_rule_exit"

    def explain_decision(self, context: StrategyContext) -> Mapping[str, object]:
        """Return only values calculated from this completed-bar context.

        The bridge currently serializes mapping values; retaining a canonical
        JSON record means old trace storage remains readable while native trace
        models transition to typed values.  No later bar or post-trade outcome
        participates in this record.
        """

        return {
            "rule_phase": self._last_rule_trace["phase"],
            "rule_outcome": self._last_rule_trace["outcome"],
            "rule_trace": json.dumps(self._last_rule_trace, sort_keys=True, default=str),
            "rule_known_at": context.timestamp.isoformat(),
        }


def _lab_rule_matches(frame: pd.DataFrame, raw: object) -> bool | None:
    return _lab_rule_diagnostic(frame, raw)["outcome"]  # type: ignore[return-value]


def _lab_rule_diagnostic(frame: pd.DataFrame, raw: object) -> dict[str, object]:
    """Evaluate and describe a rule from values known at the active close."""

    if not isinstance(raw, Mapping) or frame.empty:
        return {"kind": "invalid", "outcome": None, "reason": "rule or completed bars unavailable"}
    kind = raw.get("kind")
    if kind == "comparison":
        left, left_detail = _lab_indicator_diagnostic(frame, raw.get("left"))
        right, right_detail = _lab_operand_diagnostic(frame, raw.get("right"))
        if left is None or right is None:
            return {
                "kind": "comparison",
                "outcome": None,
                "comparator": raw.get("comparator"),
                "left": left_detail,
                "right": right_detail,
                "reason": "required value is unavailable or warming up",
            }
        comparator = raw.get("comparator")
        if not isinstance(comparator, str):
            return {"kind": "comparison", "outcome": None, "reason": "invalid comparator"}
        outcome = {
            ">": left > right,
            ">=": left >= right,
            "<": left < right,
            "<=": left <= right,
            "==": left == right,
        }.get(comparator)
        return {
            "kind": "comparison",
            "outcome": outcome,
            "comparator": comparator,
            "left": left_detail,
            "right": right_detail,
        }
    if kind == "cross":
        left = _lab_indicator_series(frame, raw.get("left"))
        right = _lab_indicator_series(frame, raw.get("right"))
        if left is None or right is None or len(left) < 2 or len(right) < 2:
            return {"kind": "cross", "outcome": None, "reason": "two completed values are required"}
        previous = (float(left.iloc[-2]), float(right.iloc[-2]))
        current = (float(left.iloc[-1]), float(right.iloc[-1]))
        if any(pd.isna(value) for value in (*previous, *current)):
            return {"kind": "cross", "outcome": None, "reason": "indicator is warming up"}
        outcome = (
            previous[0] <= previous[1] and current[0] > current[1]
            if raw.get("direction") == "above"
            else previous[0] >= previous[1] and current[0] < current[1]
        )
        return {
            "kind": "cross",
            "outcome": outcome,
            "direction": raw.get("direction"),
            "previous": {"left": previous[0], "right": previous[1]},
            "current": {"left": current[0], "right": current[1]},
        }
    if kind == "sustained_for":
        child = raw.get("child")
        bars = _integer(raw.get("bars"), 1)
        if bars < 1 or len(frame) < bars:
            return {"kind": "sustained_for", "outcome": None, "reason": "insufficient completed bars"}
        values = [_lab_rule_matches(frame.iloc[:end], child) for end in range(len(frame) - bars + 1, len(frame) + 1)]
        return {
            "kind": "sustained_for",
            "outcome": _and(values),
            "bars": bars,
            "child_outcomes": values,
        }
    children = raw.get("children")
    if not isinstance(children, list) or not children:
        return {"kind": str(kind), "outcome": None, "reason": "rule group has no children"}
    diagnostics = [_lab_rule_diagnostic(frame, child) for child in children]
    results = [child["outcome"] for child in diagnostics]
    if kind == "and":
        outcome = _and(results)  # type: ignore[arg-type]
    elif kind == "or":
        outcome = _or(results)  # type: ignore[arg-type]
    elif kind == "not":
        outcome = None if len(results) != 1 or results[0] is None else not results[0]
    else:
        raise ValueError(f"unsupported lab rule kind: {kind}")
    return {"kind": kind, "outcome": outcome, "children": diagnostics}


def _lab_operand(frame: pd.DataFrame, raw: object) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return _lab_indicator(frame, raw)


def _lab_operand_diagnostic(frame: pd.DataFrame, raw: object) -> tuple[float | None, object]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw), {"kind": "literal", "value": float(raw)}
    return _lab_indicator_diagnostic(frame, raw)


def _lab_indicator(frame: pd.DataFrame, raw: object) -> float | None:
    series = _lab_indicator_series(frame, raw)
    if series is None or series.empty or pd.isna(series.iloc[-1]):
        return None
    return float(series.iloc[-1])


def _lab_indicator_diagnostic(frame: pd.DataFrame, raw: object) -> tuple[float | None, object]:
    value = _lab_indicator(frame, raw)
    if not isinstance(raw, Mapping):
        return value, {"kind": "invalid_indicator"}
    return value, {
        "kind": "indicator",
        "indicator": raw.get("indicator"),
        "timeframe": raw.get("timeframe", "current"),
        "parameters": dict(raw.get("parameters", {})) if isinstance(raw.get("parameters"), Mapping) else {},
        "value": value,
    }


def _lab_indicator_series(frame: pd.DataFrame, raw: object) -> pd.Series | None:
    if not isinstance(raw, Mapping):
        return None
    timeframe = raw.get("timeframe", "current")
    if timeframe != "current":
        raise ValueError(
            "multi-timeframe lab rules require completed, independently versioned bars; "
            "this executor will not substitute the current timeframe"
        )
    indicator = raw.get("indicator")
    parameters = raw.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise ValueError("indicator parameters must be an object")
    if indicator == "sma":
        window = _integer(parameters.get("window"), 20)
        return pd.to_numeric(frame["close"], errors="coerce").rolling(window).mean()
    if indicator == "rsi":
        from qt.indicators.talib_standard import rsi

        period = _integer(parameters.get("period"), 14)
        return rsi(pd.to_numeric(frame["close"], errors="coerce"), period=period)
    raise ValueError(f"unsupported executable lab indicator: {indicator}")


def _and(values: list[bool | None]) -> bool | None:
    if any(value is False for value in values):
        return False
    return None if any(value is None for value in values) else True


def _or(values: list[bool | None]) -> bool | None:
    if any(value is True for value in values):
        return True
    return None if any(value is None for value in values) else False


def _bind_lab_parameters(
    content: Mapping[str, object], overrides: Mapping[str, object]
) -> dict[str, object]:
    """Bind only explicit ``${parameter}`` references and reject no-op trials."""

    raw_definitions = content.get("parameters", [])
    definitions = raw_definitions if isinstance(raw_definitions, list) else []
    declared = {
        str(item.get("name")): item.get("value")
        for item in definitions
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    unknown = set(overrides).difference(declared)
    if unknown:
        raise ValueError(f"unknown lab parameter overrides: {', '.join(sorted(unknown))}")
    bound = {**declared, **dict(overrides)}
    references: set[str] = set()

    def substitute(value: object) -> object:
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            name = value[2:-1]
            if name not in bound:
                raise ValueError(f"undefined lab parameter reference: {name}")
            references.add(name)
            return bound[name]
        if isinstance(value, Mapping):
            return {str(key): substitute(item) for key, item in value.items()}
        if isinstance(value, list):
            return [substitute(item) for item in value]
        return value

    result = substitute(content)
    if not isinstance(result, dict):
        raise ValueError("lab strategy content must be an object")
    unbound = set(overrides).difference(references)
    if unbound:
        raise ValueError(
            "parameter overrides must be referenced as ${name} in a rule: "
            f"{', '.join(sorted(unbound))}"
        )
    return result


def _lab_warmup(content: Mapping[str, object]) -> int:
    windows: list[int] = []

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            parameters = value.get("parameters")
            if isinstance(parameters, Mapping):
                for key in ("window", "period"):
                    if key in parameters:
                        windows.append(_integer(parameters[key], 1))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(content)
    return max(windows, default=1) + 1
