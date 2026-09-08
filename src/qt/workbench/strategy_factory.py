"""Immutable, causal construction of native strategies from lab versions.

The factory is deliberately separate from the API/service layer.  A worker
supplies a persisted immutable version (and, for composites, a version
resolver), receives a ``btc_backtest`` strategy, and decides whether to run it
in a research engine.  This module neither submits orders nor starts qt5 live
or simulator workflows.

``catalog:`` identities are never translated to ``CatalogProfileStrategy``:
that class is an explicitly deprecated target-weight approximation.  Catalog
and qt5 source families need a caller-injected, version-pinned constructor.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Literal, Protocol, TypeAlias, cast

import pandas as pd
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    Strategy,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.registry import default_strategy_registry
from btc_backtest.strategies.target_weight import TargetWeightStrategy

from qt.indicators import price as price_indicators
from qt.indicators import talib_standard
from qt.indicators import volatility as volatility_indicators
from qt.workbench.catalog_signals import add_catalog_indicators, evaluate_signal

JsonDict: TypeAlias = dict[str, object]
VersionResolver = Callable[[str], Mapping[str, object]]


class DedicatedSourceConstructor(Protocol):
    """Build a source-native family without changing its semantic identity."""

    def __call__(
        self,
        version: Mapping[str, object],
        parameters: Mapping[str, object],
    ) -> Strategy: ...


@dataclass(frozen=True)
class FactoryLimits:
    """Bound a persisted composite graph before it reaches a native engine."""

    max_depth: int = 4
    max_leaf_strategies: int = 3

    def __post_init__(self) -> None:
        if self.max_depth < 1:
            raise ValueError("max_depth must be positive")
        if self.max_leaf_strategies < 1:
            raise ValueError("max_leaf_strategies must be positive")


DEFAULT_FACTORY_LIMITS = FactoryLimits()


@dataclass(frozen=True)
class StrategyDependency:
    version_id: str
    source_family: str
    identity: str
    version: str


@dataclass(frozen=True)
class BuiltLabStrategy:
    """Immutable construction record consumed by the research worker."""

    strategy: Strategy
    dependency: StrategyDependency
    dependencies: tuple[StrategyDependency, ...]
    parameter_bindings: Mapping[str, object]


@dataclass(frozen=True)
class _BuildState:
    ancestry: frozenset[str]
    depth: int
    leaves: list[int]


def build_lab_strategy(
    version: Mapping[str, object],
    *,
    version_resolver: VersionResolver | None = None,
    source_constructor: DedicatedSourceConstructor | None = None,
    parameter_overrides: Mapping[str, object] | None = None,
    primary_timeframe: str = "1h",
    limits: FactoryLimits = DEFAULT_FACTORY_LIMITS,
) -> BuiltLabStrategy:
    """Build an exact native strategy from a persisted, immutable lab version.

    ``version`` must include ``version_id`` and ``content``.  Child versions
    may be embedded as ``strategy_version`` or resolved by id.  All parameter
    substitutions use full ``${name}`` tokens; unknown or unused overrides are
    errors rather than silently changing research semantics.
    """

    return _build(
        version,
        version_resolver=version_resolver,
        source_constructor=source_constructor,
        parameter_overrides=parameter_overrides or {},
        primary_timeframe=primary_timeframe,
        limits=limits,
        state=_BuildState(ancestry=frozenset(), depth=0, leaves=[0]),
    )


def _build(
    version: Mapping[str, object],
    *,
    version_resolver: VersionResolver | None,
    source_constructor: DedicatedSourceConstructor | None,
    parameter_overrides: Mapping[str, object],
    primary_timeframe: str,
    limits: FactoryLimits,
    state: _BuildState,
) -> BuiltLabStrategy:
    version_id = _required_text(version.get("version_id"), "strategy version_id")
    if version_id in state.ancestry:
        raise ValueError(f"strategy version graph contains a cycle at {version_id}")
    if state.depth >= limits.max_depth:
        raise ValueError(f"strategy version graph exceeds maximum depth {limits.max_depth}")
    content = _mapping(version.get("content"), "strategy version content")
    mode = _required_text(content.get("mode"), "strategy version content.mode")
    bound_content, bindings = _bind_parameters(content, parameter_overrides)
    next_state = _BuildState(
        ancestry=state.ancestry | {version_id},
        depth=state.depth + 1,
        leaves=state.leaves,
    )

    if mode == "builtin":
        _claim_leaf(state, limits)
        return _build_builtin(
            version_id,
            bound_content,
            bindings,
            source_version={"version_id": version_id, "content": bound_content},
            source_constructor=source_constructor,
        )
    if mode == "rules":
        _claim_leaf(state, limits)
        strategy = ImmutableRuleStrategy(
            version_id=version_id,
            content=bound_content,
            primary_timeframe=primary_timeframe,
        )
        dependency = StrategyDependency(
            version_id=version_id,
            source_family="lab_rules",
            identity=strategy.metadata.id,
            version=strategy.metadata.version,
        )
        return BuiltLabStrategy(
            strategy=strategy,
            dependency=dependency,
            dependencies=(dependency,),
            parameter_bindings=_frozen_mapping(bindings),
        )
    if mode == "ensemble":
        return _build_ensemble(
            version_id,
            bound_content,
            bindings,
            version_resolver=version_resolver,
            source_constructor=source_constructor,
            primary_timeframe=primary_timeframe,
            limits=limits,
            state=next_state,
        )
    if mode == "regime_switch":
        return _build_regime_switch(
            version_id,
            bound_content,
            bindings,
            version_resolver=version_resolver,
            source_constructor=source_constructor,
            primary_timeframe=primary_timeframe,
            limits=limits,
            state=next_state,
        )
    raise ValueError(f"unsupported executable lab mode: {mode}")


def _build_builtin(
    version_id: str,
    content: Mapping[str, object],
    bindings: Mapping[str, object],
    *,
    source_version: Mapping[str, object],
    source_constructor: DedicatedSourceConstructor | None,
) -> BuiltLabStrategy:
    identity = _required_text(content.get("builtin_identity"), "builtin_identity")
    expected_version = _required_text(content.get("builtin_version"), "builtin_version")
    parameters = _mapping_or_empty(content.get("builtin_parameters"), "builtin_parameters")
    if identity.startswith("qt:"):
        strategy_id = identity.removeprefix("qt:")
        registry = default_strategy_registry()
        metadata = registry.describe(strategy_id)
        if metadata.version != expected_version:
            raise ValueError(
                f"builtin version mismatch for {identity}: expected {expected_version}, got {metadata.version}"
            )
        strategy = registry.create(strategy_id, parameters)
        dependency = StrategyDependency(
            version_id=version_id,
            source_family="btc_backtest",
            identity=identity,
            version=metadata.version,
        )
        return BuiltLabStrategy(
            strategy=strategy,
            dependency=dependency,
            dependencies=(dependency,),
            parameter_bindings=_frozen_mapping(bindings),
        )
    if source_constructor is None:
        raise ValueError(
            f"{identity} requires an injected dedicated source constructor; "
            "the immutable factory will not substitute a target-weight proxy"
        )
    strategy = source_constructor(
        _frozen_mapping(source_version),
        _frozen_mapping(parameters),
    )
    if not isinstance(strategy, Strategy):
        raise TypeError("dedicated source constructor did not return a native Strategy")
    dependency = StrategyDependency(
        version_id=version_id,
        source_family=_source_family(identity),
        identity=identity,
        version=expected_version,
    )
    return BuiltLabStrategy(
        strategy=strategy,
        dependency=dependency,
        dependencies=(dependency,),
        parameter_bindings=_frozen_mapping(bindings),
    )


def _build_ensemble(
    version_id: str,
    content: Mapping[str, object],
    bindings: Mapping[str, object],
    *,
    version_resolver: VersionResolver | None,
    source_constructor: DedicatedSourceConstructor | None,
    primary_timeframe: str,
    limits: FactoryLimits,
    state: _BuildState,
) -> BuiltLabStrategy:
    members = content.get("ensemble")
    if not isinstance(members, list) or not 2 <= len(members) <= 3:
        raise ValueError("ensemble requires two or three immutable members")
    children: list[BuiltLabStrategy] = []
    weights: list[Decimal] = []
    for member in members:
        item = _mapping(member, "ensemble member")
        child = _build_child(
            item,
            version_resolver=version_resolver,
            source_constructor=source_constructor,
            primary_timeframe=primary_timeframe,
            limits=limits,
            state=state,
        )
        children.append(child)
        weights.append(_decimal(item.get("target_weight"), "ensemble target_weight"))
    if any(not isinstance(child.strategy, TargetWeightStrategy) for child in children):
        raise ValueError("ensemble members must implement native TargetWeightStrategy")
    if sum(weights, Decimal("0")) != Decimal("1"):
        raise ValueError("ensemble target weights must sum exactly to one")
    strategy = ImmutableWeightedEnsemble(
        tuple(
            (cast(TargetWeightStrategy, child.strategy), weight)
            for child, weight in zip(children, weights, strict=True)
        ),
        version_id=version_id,
    )
    dependency = StrategyDependency(version_id, "lab_ensemble", "weighted_ensemble", "1")
    return BuiltLabStrategy(
        strategy=strategy,
        dependency=dependency,
        dependencies=(dependency, *(dependency for child in children for dependency in child.dependencies)),
        parameter_bindings=_frozen_mapping(bindings),
    )


def _build_regime_switch(
    version_id: str,
    content: Mapping[str, object],
    bindings: Mapping[str, object],
    *,
    version_resolver: VersionResolver | None,
    source_constructor: DedicatedSourceConstructor | None,
    primary_timeframe: str,
    limits: FactoryLimits,
    state: _BuildState,
) -> BuiltLabStrategy:
    branches = content.get("regimes")
    if not isinstance(branches, list) or not 1 <= len(branches) <= 3:
        raise ValueError("regime_switch requires one to three immutable branches")
    built_branches: list[_RegimeBranch] = []
    seen_names: set[str] = set()
    for branch in branches:
        item = _mapping(branch, "regime branch")
        name = _required_text(item.get("name"), "regime branch name")
        if name in seen_names:
            raise ValueError(f"regime branch name is duplicated: {name}")
        seen_names.add(name)
        rule = _mapping(item.get("when"), "regime branch when")
        child = _build_child(
            item,
            version_resolver=version_resolver,
            source_constructor=source_constructor,
            primary_timeframe=primary_timeframe,
            limits=limits,
            state=state,
        )
        if not isinstance(child.strategy, TargetWeightStrategy):
            raise ValueError("regime branches must implement native TargetWeightStrategy")
        built_branches.append(
            _RegimeBranch(
                name=name,
                when=_frozen_mapping(rule),
                strategy=cast(TargetWeightStrategy, child.strategy),
                dependencies=child.dependencies,
            )
        )
    strategy = ImmutableRegimeSwitch(
        tuple(built_branches),
        version_id=version_id,
        primary_timeframe=primary_timeframe,
    )
    dependency = StrategyDependency(version_id, "lab_regime_switch", "regime_switch", "1")
    return BuiltLabStrategy(
        strategy=strategy,
        dependency=dependency,
        dependencies=(
            dependency,
            *(dependency for branch in built_branches for dependency in branch.dependencies),
        ),
        parameter_bindings=_frozen_mapping(bindings),
    )


def _build_child(
    item: Mapping[str, object],
    *,
    version_resolver: VersionResolver | None,
    source_constructor: DedicatedSourceConstructor | None,
    primary_timeframe: str,
    limits: FactoryLimits,
    state: _BuildState,
) -> BuiltLabStrategy:
    embedded = item.get("strategy_version")
    if isinstance(embedded, Mapping):
        child_version = embedded
    else:
        child_id = _required_text(item.get("strategy_version_id"), "child strategy_version_id")
        if version_resolver is None:
            raise ValueError("composite strategy construction requires a version resolver")
        child_version = version_resolver(child_id)
    return _build(
        child_version,
        version_resolver=version_resolver,
        source_constructor=source_constructor,
        parameter_overrides={},
        primary_timeframe=primary_timeframe,
        limits=limits,
        state=state,
    )


def _dependencies_for(strategy: TargetWeightStrategy) -> tuple[StrategyDependency, ...]:
    dependencies = getattr(strategy, "_lab_dependencies", ())
    return dependencies if isinstance(dependencies, tuple) else ()


class ImmutableRuleStrategy(TargetWeightStrategy):  # type: ignore[misc]
    """Native target-weight strategy evaluating the immutable typed Lab AST."""

    def __init__(self, *, version_id: str, content: Mapping[str, object], primary_timeframe: str) -> None:
        super().__init__()
        self._content = _frozen_mapping(_copy_mapping(content))
        self._primary_timeframe = _validate_timeframe(primary_timeframe)
        self._target = Decimal("0")
        self._last_trace: JsonDict = {"outcome": None, "reason": "no completed bar evaluated"}
        self.metadata = StrategyMetadata(
            id="lab_rule_version",
            version=version_id,
            description="Immutable Lab rule strategy evaluated on completed causal bars.",
            warmup_bars=_rule_warmup(content),
            supported_timeframes=("1h", "1d"),
            requires_full_history=False,
        )

    def target_weight(self, context: StrategyContext) -> Decimal:
        phase = "entry" if self._target == 0 else "exit"
        raw_rule = self._content.get("entry_rule") if phase == "entry" else self._content.get("exit_rule")
        trace = _evaluate_rule(context, raw_rule, primary_timeframe=self._primary_timeframe)
        self._last_trace = {"phase": phase, **trace}
        if trace["outcome"] is True:
            self._target = Decimal("1") if phase == "entry" else Decimal("0")
        return self._target

    def rebalance_reason(self, *, current_value: Decimal, target_value: Decimal) -> str:
        return "lab_rule_entry" if target_value > current_value else "lab_rule_exit"

    def explain_decision(self, context: StrategyContext) -> Mapping[str, object]:
        return {
            "version_id": self.metadata.version,
            "known_at": context.timestamp.isoformat(),
            "rule": _copy_mapping(self._last_trace),
        }


class ImmutableWeightedEnsemble(TargetWeightStrategy):  # type: ignore[misc]
    def __init__(self, components: tuple[tuple[TargetWeightStrategy, Decimal], ...], *, version_id: str) -> None:
        super().__init__()
        self._components = components
        self._last_trace: JsonDict = {"outcome": None, "reason": "no completed bar evaluated"}
        self._lab_dependencies = tuple(
            dependency
            for strategy, _ in components
            for dependency in _dependencies_for(strategy)
        )
        timeframes = set(components[0][0].metadata.supported_timeframes)
        for strategy, _ in components[1:]:
            timeframes.intersection_update(strategy.metadata.supported_timeframes)
        if not timeframes:
            raise ValueError("ensemble components do not share a supported timeframe")
        self.metadata = StrategyMetadata(
            id="lab_weighted_ensemble",
            version=version_id,
            description="Immutable weighted composition of native Lab strategies.",
            warmup_bars=max(strategy.metadata.warmup_bars for strategy, _ in components),
            supported_timeframes=tuple(sorted(timeframes)),
            requires_full_history=any(strategy.metadata.requires_full_history for strategy, _ in components),
        )

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        for strategy, _ in self._components:
            strategy.initialize(context)

    def target_weight(self, context: StrategyContext) -> Decimal:
        values = [
            {"strategy": strategy.metadata.id, "version": strategy.metadata.version, "weight": str(weight), "target": str(strategy.target_weight(context))}
            for strategy, weight in self._components
        ]
        target = sum(
            (weight * Decimal(item["target"]) for item, (_, weight) in zip(values, self._components, strict=True)),
            Decimal("0"),
        )
        self._last_trace = {"outcome": True, "components": values, "target": str(target)}
        return target

    def rebalance_reason(self, *, current_value: Decimal, target_value: Decimal) -> str:
        return "lab_weighted_ensemble_increase" if target_value > current_value else "lab_weighted_ensemble_decrease"

    def finalize(self, context: FinalizationContext) -> None:
        for strategy, _ in self._components:
            strategy.finalize(context)

    def explain_decision(self, context: StrategyContext) -> Mapping[str, object]:
        return {"known_at": context.timestamp.isoformat(), "ensemble": _copy_mapping(self._last_trace)}


@dataclass(frozen=True)
class _RegimeBranch:
    name: str
    when: Mapping[str, object]
    strategy: TargetWeightStrategy
    dependencies: tuple[StrategyDependency, ...]


class ImmutableRegimeSwitch(TargetWeightStrategy):  # type: ignore[misc]
    def __init__(
        self,
        branches: tuple[_RegimeBranch, ...],
        *,
        version_id: str,
        primary_timeframe: str,
    ) -> None:
        super().__init__()
        self._branches = branches
        self._primary_timeframe = _validate_timeframe(primary_timeframe)
        self._target = Decimal("0")
        self._last_trace: JsonDict = {"outcome": None, "reason": "no completed bar evaluated"}
        self._lab_dependencies = tuple(
            dependency for branch in branches for dependency in _dependencies_for(branch.strategy)
        )
        timeframes = set(branches[0].strategy.metadata.supported_timeframes)
        for branch in branches[1:]:
            timeframes.intersection_update(branch.strategy.metadata.supported_timeframes)
        if not timeframes:
            raise ValueError("regime branches do not share a supported timeframe")
        self.metadata = StrategyMetadata(
            id="lab_regime_switch",
            version=version_id,
            description="Immutable causal regime selection over native Lab strategies.",
            warmup_bars=max(branch.strategy.metadata.warmup_bars for branch in branches),
            supported_timeframes=tuple(sorted(timeframes)),
            requires_full_history=any(branch.strategy.metadata.requires_full_history for branch in branches),
        )

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        for branch in self._branches:
            branch.strategy.initialize(context)

    def target_weight(self, context: StrategyContext) -> Decimal:
        traces: list[JsonDict] = []
        for branch in self._branches:
            trace = _evaluate_rule(context, branch.when, primary_timeframe=self._primary_timeframe)
            traces.append({"name": branch.name, **trace})
            if trace["outcome"] is True:
                self._target = branch.strategy.target_weight(context)
                self._last_trace = {"outcome": True, "selected": branch.name, "branches": traces}
                return self._target
        self._last_trace = {"outcome": None, "selected": None, "branches": traces, "reason": "no known regime matched"}
        return self._target

    def rebalance_reason(self, *, current_value: Decimal, target_value: Decimal) -> str:
        return "lab_regime_switch_increase" if target_value > current_value else "lab_regime_switch_decrease"

    def finalize(self, context: FinalizationContext) -> None:
        for branch in self._branches:
            branch.strategy.finalize(context)

    def explain_decision(self, context: StrategyContext) -> Mapping[str, object]:
        return {"known_at": context.timestamp.isoformat(), "regime": _copy_mapping(self._last_trace)}


def _evaluate_rule(
    context: StrategyContext,
    raw: object,
    *,
    primary_timeframe: str,
) -> JsonDict:
    if not isinstance(raw, Mapping):
        return {"outcome": None, "reason": "rule is unavailable"}
    kind = raw.get("kind")
    if kind == "comparison":
        left, left_trace = _operand(context, raw.get("left"), primary_timeframe=primary_timeframe)
        right, right_trace = _operand(context, raw.get("right"), primary_timeframe=primary_timeframe)
        comparator = raw.get("comparator")
        if left is None or right is None:
            return {"kind": "comparison", "outcome": None, "left": left_trace, "right": right_trace, "reason": "required value is unknown"}
        if comparator not in {">", ">=", "<", "<=", "=="}:
            return {"kind": "comparison", "outcome": None, "reason": "invalid comparator"}
        outcome = {
            ">": left > right,
            ">=": left >= right,
            "<": left < right,
            "<=": left <= right,
            "==": left == right,
        }[cast(Literal[">", ">=", "<", "<=", "=="], comparator)]
        return {"kind": "comparison", "outcome": outcome, "comparator": comparator, "left": left_trace, "right": right_trace}
    if kind == "cross":
        left, left_trace = _indicator_series(context, raw.get("left"), primary_timeframe=primary_timeframe)
        right, right_trace = _indicator_series(context, raw.get("right"), primary_timeframe=primary_timeframe)
        if left is None or right is None or len(left) < 2 or len(right) < 2:
            return {"kind": "cross", "outcome": None, "reason": "two completed indicator values are required"}
        previous = (left.iloc[-2], right.iloc[-2])
        current = (left.iloc[-1], right.iloc[-1])
        if any(not _finite(value) for value in (*previous, *current)):
            return {"kind": "cross", "outcome": None, "left": left_trace, "right": right_trace, "reason": "indicator is warming up"}
        direction = raw.get("direction")
        cross_outcome: bool | None
        if direction == "above":
            cross_outcome = previous[0] <= previous[1] and current[0] > current[1]
        elif direction == "below":
            cross_outcome = previous[0] >= previous[1] and current[0] < current[1]
        else:
            cross_outcome = None
        return {"kind": "cross", "outcome": cross_outcome, "direction": direction, "previous": {"left": float(previous[0]), "right": float(previous[1])}, "current": {"left": float(current[0]), "right": float(current[1])}}
    if kind == "sustained_for":
        bars = _positive_int(raw.get("bars"), "sustained_for bars")
        if len(context.bars) < bars:
            return {"kind": "sustained_for", "outcome": None, "reason": "insufficient completed bars"}
        outcomes: list[bool | None] = []
        for end in range(len(context.bars) - bars + 1, len(context.bars) + 1):
            sliced = context.model_copy(update={"bars": context.bars.iloc[:end]})
            child_trace = _evaluate_rule(
                sliced,
                raw.get("child"),
                primary_timeframe=primary_timeframe,
            )
            child_outcome = child_trace.get("outcome")
            outcomes.append(child_outcome if isinstance(child_outcome, bool) else None)
        return {"kind": "sustained_for", "outcome": _tri_and(outcomes), "bars": bars, "child_outcomes": outcomes}
    children = raw.get("children")
    if not isinstance(children, list) or not children:
        return {"kind": str(kind), "outcome": None, "reason": "rule group has no children"}
    traces = [_evaluate_rule(context, child, primary_timeframe=primary_timeframe) for child in children]
    group_outcomes: list[bool | None] = [_rule_outcome(trace) for trace in traces]
    group_outcome: bool | None
    if kind == "and":
        group_outcome = _tri_and(group_outcomes)
    elif kind == "or":
        group_outcome = _tri_or(group_outcomes)
    elif kind == "not" and len(group_outcomes) == 1:
        group_outcome = None if group_outcomes[0] is None else not group_outcomes[0]
    else:
        group_outcome = None
    return {"kind": kind, "outcome": group_outcome, "children": traces}


def _operand(context: StrategyContext, raw: object, *, primary_timeframe: str) -> tuple[float | None, JsonDict]:
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw), {"kind": "literal", "value": float(raw)}
    series, trace = _indicator_series(context, raw, primary_timeframe=primary_timeframe)
    if series is None or series.empty or not _finite(series.iloc[-1]):
        return None, trace
    return float(series.iloc[-1]), {**trace, "value": float(series.iloc[-1])}


def _indicator_series(
    context: StrategyContext,
    raw: object,
    *,
    primary_timeframe: str,
) -> tuple[pd.Series | None, JsonDict]:
    if not isinstance(raw, Mapping):
        return None, {"kind": "indicator", "reason": "invalid indicator"}
    indicator = _required_text(raw.get("indicator"), "rule indicator")
    timeframe = _required_text(raw.get("timeframe", "current"), "rule indicator timeframe")
    parameters = _mapping_or_empty(raw.get("parameters"), "rule indicator parameters")
    try:
        frame = _frame_for_indicator(context, timeframe, primary_timeframe=primary_timeframe)
        series = _indicator(frame, indicator, parameters)
    except (KeyError, TypeError, ValueError) as error:
        return None, {"kind": "indicator", "indicator": indicator, "timeframe": timeframe, "parameters": dict(parameters), "reason": str(error)}
    return series, {"kind": "indicator", "indicator": indicator, "timeframe": timeframe, "parameters": dict(parameters)}


def _frame_for_indicator(context: StrategyContext, timeframe: str, *, primary_timeframe: str) -> pd.DataFrame:
    active = pd.Timestamp(context.timestamp)
    if timeframe == "current":
        return context.bars.loc[context.bars.index <= active].copy(deep=True)
    target_seconds = _timeframe_seconds(timeframe)
    primary_seconds = _timeframe_seconds(primary_timeframe)
    if target_seconds <= primary_seconds:
        raise ValueError("multi-timeframe indicators must be coarser than the primary timeframe")
    external = context.auxiliary.get(timeframe)
    if external is not None:
        return external.loc[external.index <= active].copy(deep=True)
    bars = context.bars.loc[context.bars.index <= active]
    if bars.empty:
        return bars.copy(deep=True)
    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(bars.columns)
    if missing:
        raise ValueError(f"cannot resample multi-timeframe bars missing {', '.join(sorted(missing))}")
    resampled = bars.resample(timeframe, label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return resampled.dropna().loc[lambda frame: frame.index <= active].copy(deep=True)


def _indicator(frame: pd.DataFrame, name: str, parameters: Mapping[str, object]) -> pd.Series:
    close = _numeric_column(frame, "close")
    if name == "close":
        return close
    if name == "sma":
        return close.rolling(_positive_int(parameters.get("window", 20), "sma window")).mean()
    if name == "ema":
        return close.ewm(span=_positive_int(parameters.get("window", 20), "ema window"), adjust=False).mean()
    if name in {"rsi", "talib_standard.rsi"}:
        return talib_standard.rsi(close, period=_positive_int(parameters.get("period", 14), "rsi period"))
    if name == "price.rsi":
        return price_indicators.rsi(close, period=_positive_int(parameters.get("period", 14), "rsi period"))
    high = _numeric_column(frame, "high")
    low = _numeric_column(frame, "low")
    if name in {"atr", "talib_standard.atr"}:
        return talib_standard.atr(high, low, close, period=_positive_int(parameters.get("period", 14), "atr period"))
    if name in {"adx", "talib_standard.adx"}:
        return talib_standard.adx(high, low, close, period=_positive_int(parameters.get("period", 14), "adx period"))
    if name == "price.atr":
        return price_indicators.atr(high, low, close, period=_positive_int(parameters.get("period", 14), "atr period"))
    if name == "price.drawdown_from_high":
        return price_indicators.drawdown_from_high(close, window=_positive_int(parameters.get("window", 720), "drawdown window"))
    if name == "price.wick_ratio":
        return price_indicators.wick_ratio(_numeric_column(frame, "open"), high, low, close)
    if name == "volatility.realized_vol":
        return volatility_indicators.realized_vol(close, window=_positive_int(parameters.get("window", 24), "realized_vol window"))
    if name == "catalog_signal":
        signal_id = _required_text(parameters.get("id"), "catalog_signal id")
        signal_params = _mapping_or_empty(parameters.get("signal_parameters"), "catalog_signal signal_parameters")
        enriched = add_catalog_indicators(frame)
        return evaluate_signal(signal_id, enriched, signal_params).astype(float)
    raise ValueError(f"unsupported immutable lab indicator: {name}")


def _bind_parameters(content: Mapping[str, object], overrides: Mapping[str, object]) -> tuple[JsonDict, JsonDict]:
    definitions = content.get("parameters", [])
    if not isinstance(definitions, list):
        raise ValueError("content.parameters must be a list")
    declared: JsonDict = {}
    for definition in definitions:
        item = _mapping(definition, "parameter definition")
        name = _required_text(item.get("name"), "parameter name")
        if name in declared:
            raise ValueError(f"duplicate parameter definition: {name}")
        declared[name] = item.get("value")
    unknown = set(overrides).difference(declared)
    if unknown:
        raise ValueError(f"unknown parameter bindings: {', '.join(sorted(unknown))}")
    values = {**declared, **dict(overrides)}
    references: set[str] = set()

    def substitute(value: object) -> object:
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            name = value[2:-1]
            if name not in values:
                raise ValueError(f"undefined parameter reference: {name}")
            references.add(name)
            return _copy_value(values[name])
        if isinstance(value, Mapping):
            return {str(key): substitute(item) for key, item in value.items()}
        if isinstance(value, list):
            return [substitute(item) for item in value]
        return _copy_value(value)

    bound = substitute(content)
    if not isinstance(bound, dict):
        raise ValueError("strategy content must be an object")
    unused = set(overrides).difference(references)
    if unused:
        raise ValueError(f"parameter bindings must be referenced: {', '.join(sorted(unused))}")
    return bound, {name: _copy_value(values[name]) for name in sorted(references)}


def _rule_warmup(content: Mapping[str, object]) -> int:
    windows: list[int] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            parameters = value.get("parameters")
            if isinstance(parameters, Mapping):
                for key in ("window", "period", "lookback"):
                    if key in parameters and isinstance(parameters[key], int) and not isinstance(parameters[key], bool):
                        windows.append(parameters[key])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(content)
    return max(windows, default=1) + 1


def _source_family(identity: str) -> str:
    if identity.startswith("catalog:"):
        return "btc_quant_catalog"
    if identity.startswith("qt5live:"):
        return "qt5_live"
    if identity.startswith("qt5sim:"):
        return "qt5_sim"
    return "external_source"


def _claim_leaf(state: _BuildState, limits: FactoryLimits) -> None:
    if state.leaves[0] >= limits.max_leaf_strategies:
        raise ValueError(
            "composite execution graph may contain at most "
            f"{limits.max_leaf_strategies} leaf strategies"
        )
    state.leaves[0] += 1


def _tri_and(values: Sequence[bool | None]) -> bool | None:
    if any(value is False for value in values):
        return False
    return None if any(value is None for value in values) else True


def _tri_or(values: Sequence[bool | None]) -> bool | None:
    if any(value is True for value in values):
        return True
    return None if any(value is None for value in values) else False


def _rule_outcome(trace: Mapping[str, object]) -> bool | None:
    value = trace.get("outcome")
    return value if isinstance(value, bool) else None


def _numeric_column(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame:
        raise ValueError(f"indicator requires {name} column")
    return pd.to_numeric(frame[name], errors="coerce")


def _finite(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and pd.notna(value) and float(value) == float(value) and abs(float(value)) != float("inf")


def _timeframe_seconds(value: str) -> int:
    return _positive_int(value[:-1], "timeframe") * {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(value[-1:], 0)


def _validate_timeframe(value: str) -> str:
    if len(value) < 2 or _timeframe_seconds(value) <= 0:
        raise ValueError(f"unsupported timeframe: {value}")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        result = int(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a positive integer") from error
    if result <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return result


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be positive and finite")
    return result


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def _mapping_or_empty(value: object, field: str) -> Mapping[str, object]:
    return {} if value is None else _mapping(value, field)


def _copy_mapping(value: Mapping[str, object]) -> JsonDict:
    return {str(key): _copy_value(item) for key, item in value.items()}


def _copy_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _copy_mapping(cast(Mapping[str, object], value))
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    return value


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(_copy_mapping(value))


__all__ = [
    "BuiltLabStrategy",
    "DedicatedSourceConstructor",
    "FactoryLimits",
    "ImmutableRegimeSwitch",
    "ImmutableRuleStrategy",
    "ImmutableWeightedEnsemble",
    "StrategyDependency",
    "VersionResolver",
    "build_lab_strategy",
]
