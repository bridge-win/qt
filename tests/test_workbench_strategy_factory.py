from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.data.models import MarketBundle
from btc_backtest.engine.models import PortfolioSnapshot
from btc_backtest.engine.runner import EventRunner
from btc_backtest.strategies.base import Strategy, StrategyContext
from btc_backtest.strategies.registry import default_strategy_registry

from qt.workbench.strategy_factory import FactoryLimits, build_lab_strategy


def _context(frame: pd.DataFrame, at: pd.Timestamp | None = None) -> StrategyContext:
    timestamp = at or frame.index[-1]
    return StrategyContext(
        timestamp=timestamp.to_pydatetime(),
        bars=frame.loc[:timestamp],
        portfolio=PortfolioSnapshot(
            timestamp=timestamp.to_pydatetime(),
            cash=Decimal("10000"),
            equity=Decimal("10000"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(),
        ),
    )


def _frame(values: list[float]) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(values), freq="1h", tz="UTC")
    close = pd.Series(values, index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": close - 0.25,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 100.0,
        },
        index=index,
    )


def _rule_version(version_id: str, *, period: int = 2) -> dict[str, object]:
    return {
        "version_id": version_id,
        "content": {
            "mode": "rules",
            "parameters": [{"name": "period", "value": period}],
            "entry_rule": {
                "kind": "comparison",
                "left": {
                    "indicator": "sma",
                    "parameters": {"window": "${period}"},
                },
                "comparator": ">",
                "right": 2.25,
            },
            "exit_rule": {
                "kind": "comparison",
                "left": {"indicator": "close", "parameters": {}},
                "comparator": "<",
                "right": -1,
            },
        },
    }


def _builtin_version(strategy_id: str) -> dict[str, object]:
    metadata = default_strategy_registry().describe(strategy_id)
    return {
        "version_id": f"builtin-{strategy_id}",
        "content": {
            "mode": "builtin",
            "builtin_identity": f"qt:{strategy_id}",
            "builtin_version": metadata.version,
            "builtin_parameters": {},
        },
    }


def test_builtin_is_version_resolved_and_source_profiles_need_a_dedicated_constructor() -> None:
    built = build_lab_strategy(_builtin_version("sma_crossover"))
    assert built.strategy.metadata.id == "sma_crossover"
    assert built.dependency.source_family == "btc_backtest"

    mismatched = _builtin_version("sma_crossover")
    content = mismatched["content"]
    assert isinstance(content, dict)
    content["builtin_version"] = "not-the-registry-version"
    with pytest.raises(ValueError, match="version mismatch"):
        build_lab_strategy(mismatched)

    catalog = {
        "version_id": "catalog-1",
        "content": {
            "mode": "builtin",
            "builtin_identity": "catalog:trend_v1",
            "builtin_version": "btc-quant-e0b47ca",
            "builtin_parameters": {"risk": 0.01},
        },
    }
    with pytest.raises(ValueError, match="dedicated source constructor"):
        build_lab_strategy(catalog)

    def source_constructor(
        version: Mapping[str, object], parameters: Mapping[str, object]
    ) -> Strategy:
        assert version["version_id"] == "catalog-1"
        content = version["content"]
        assert isinstance(content, Mapping)
        assert content["builtin_identity"] == "catalog:trend_v1"
        assert parameters == {"risk": 0.01}
        return default_strategy_registry().create("buy_and_hold", {})

    injected = build_lab_strategy(catalog, source_constructor=source_constructor)
    assert injected.dependency.source_family == "btc_quant_catalog"
    assert injected.dependency.identity == "catalog:trend_v1"


def test_rules_bind_parameters_are_immutable_and_explain_completed_values() -> None:
    original = _rule_version("rules-1")
    fast = build_lab_strategy(original)
    slow = build_lab_strategy(original, parameter_overrides={"period": 3})
    frame = _frame([1, 2, 3])
    content = original["content"]
    assert isinstance(content, dict)
    entry = content["entry_rule"]
    assert isinstance(entry, dict)
    left = entry["left"]
    assert isinstance(left, dict)
    parameters = left["parameters"]
    assert isinstance(parameters, dict)
    parameters["window"] = 99
    fast_strategy = fast.strategy
    slow_strategy = slow.strategy
    assert fast_strategy.target_weight(_context(frame)) == Decimal("1")
    assert slow_strategy.target_weight(_context(frame)) == Decimal("0")
    assert fast.parameter_bindings == {"period": 2}
    assert slow.parameter_bindings == {"period": 3}
    explanation = fast_strategy.explain_decision(_context(frame))
    rule = explanation["rule"]
    assert isinstance(rule, Mapping)
    assert rule["outcome"] is True
    assert rule["left"] == {
        "kind": "indicator",
        "indicator": "sma",
        "timeframe": "current",
        "parameters": {"window": 2},
        "value": 2.5,
    }


def test_rule_context_is_causal_and_future_mutation_cannot_change_prior_decision() -> None:
    version = _rule_version("rules-causal")
    before = _frame([1, 2, 3, 4])
    active_at = before.index[-1]
    first = build_lab_strategy(version).strategy.target_weight(_context(before, active_at))
    later = pd.concat([before, _frame([99]).set_axis([active_at + pd.Timedelta(hours=1)])])
    later.loc[later.index[-1], "close"] = -10_000
    second = build_lab_strategy(version).strategy.target_weight(_context(later, active_at))
    assert first == second == Decimal("1")


def test_nested_ensemble_and_regime_selection_resolve_immutable_children() -> None:
    child_one = _rule_version("child-one")
    child_two = _rule_version("child-two")
    versions: dict[str, Mapping[str, object]] = {
        "child-one": child_one,
        "child-two": child_two,
    }

    def resolver(version_id: str) -> Mapping[str, object]:
        return versions[version_id]

    ensemble = {
        "version_id": "ensemble-1",
        "content": {
            "mode": "ensemble",
            "ensemble": [
                {"strategy_version_id": "child-one", "target_weight": 0.4},
                {"strategy_version_id": "child-two", "target_weight": 0.6},
            ],
        },
    }
    built_ensemble = build_lab_strategy(ensemble, version_resolver=resolver)
    assert built_ensemble.strategy.target_weight(_context(_frame([1, 2, 3]))) == Decimal("1.0")
    assert {dependency.version_id for dependency in built_ensemble.dependencies} == {
        "ensemble-1",
        "child-one",
        "child-two",
    }
    versions["ensemble-1"] = ensemble

    regime = {
        "version_id": "regime-1",
        "content": {
            "mode": "regime_switch",
            "regimes": [
                {
                    "name": "risk_on",
                    "when": {
                        "kind": "comparison",
                        "left": {"indicator": "close", "parameters": {}},
                        "comparator": ">",
                        "right": 0,
                    },
                    "strategy_version_id": "ensemble-1",
                }
            ],
        },
    }
    built_regime = build_lab_strategy(regime, version_resolver=resolver)
    assert built_regime.strategy.target_weight(_context(_frame([1, 2, 3]))) == Decimal("1")
    explanation = built_regime.strategy.explain_decision(_context(_frame([1, 2, 3])))
    state = explanation["regime"]
    assert isinstance(state, Mapping)
    assert state["selected"] == "risk_on"


def test_rule_can_use_preserved_catalog_signal_without_profile_proxy() -> None:
    version = _rule_version("catalog-signal")
    content = version["content"]
    assert isinstance(content, dict)
    entry = content["entry_rule"]
    assert isinstance(entry, dict)
    entry["left"] = {
        "indicator": "catalog_signal",
        "parameters": {"id": "close_above_ema", "signal_parameters": {"period": 2}},
    }
    entry["right"] = 0.5
    strategy = build_lab_strategy(version).strategy
    assert strategy.target_weight(_context(_frame([1, 2, 3]))) == Decimal("1")
    trace = strategy.explain_decision(_context(_frame([1, 2, 3])))["rule"]
    assert isinstance(trace, Mapping)
    assert trace["left"] == {
        "kind": "indicator",
        "indicator": "catalog_signal",
        "timeframe": "current",
        "parameters": {"id": "close_above_ema", "signal_parameters": {"period": 2}},
        "value": 1.0,
    }


def test_cycles_depth_and_unknown_multitimeframe_inputs_fail_closed() -> None:
    cyclic = {
        "version_id": "cycle",
        "content": {
            "mode": "ensemble",
            "ensemble": [{"strategy_version_id": "cycle", "target_weight": 0.5}, {"strategy_version_id": "cycle", "target_weight": 0.5}],
        },
    }
    with pytest.raises(ValueError, match="cycle"):
        build_lab_strategy(cyclic, version_resolver=lambda _: cyclic)

    depth_leaf = _rule_version("leaf")
    depth_child = {
        "version_id": "middle",
        "content": {
            "mode": "regime_switch",
            "regimes": [
                {
                    "name": "nested",
                    "when": {"kind": "comparison", "left": {"indicator": "close", "parameters": {}}, "comparator": ">", "right": 0},
                    "strategy_version_id": "leaf",
                }
            ],
        },
    }
    depth_root = {
        "version_id": "root",
        "content": {
            "mode": "regime_switch",
            "regimes": [
                {
                    "name": "nested-again",
                    "when": {"kind": "comparison", "left": {"indicator": "close", "parameters": {}}, "comparator": ">", "right": 0},
                    "strategy_version_id": "middle",
                }
            ],
        },
    }
    depth_versions: Mapping[str, Mapping[str, object]] = {
        "middle": depth_child,
        "leaf": depth_leaf,
    }

    def resolver(version_id: str) -> Mapping[str, object]:
        return depth_versions[version_id]

    with pytest.raises(ValueError, match="maximum depth"):
        build_lab_strategy(depth_root, version_resolver=resolver, limits=FactoryLimits(max_depth=2))

    multi_timeframe = _rule_version("multi")
    content = multi_timeframe["content"]
    assert isinstance(content, dict)
    entry = content["entry_rule"]
    assert isinstance(entry, dict)
    left = entry["left"]
    assert isinstance(left, dict)
    left["timeframe"] = "4h"
    strategy = build_lab_strategy(multi_timeframe).strategy
    assert strategy.target_weight(_context(_frame([1, 2, 3]))) == Decimal("0")
    trace = strategy.explain_decision(_context(_frame([1, 2, 3])))["rule"]
    assert isinstance(trace, Mapping)
    assert trace["outcome"] is None


def test_native_talib_formula_is_used_when_the_real_dependency_is_available() -> None:
    pytest.importorskip("talib.abstract")
    version = _rule_version("talib")
    content = version["content"]
    assert isinstance(content, dict)
    entry = content["entry_rule"]
    assert isinstance(entry, dict)
    entry["left"] = {"indicator": "rsi", "parameters": {"period": 2}}
    entry["right"] = 50
    frame = _frame([1, 2, 3, 4, 5, 6])
    strategy = build_lab_strategy(version).strategy
    strategy.target_weight(_context(frame))
    trace = strategy.explain_decision(_context(frame))["rule"]
    assert isinstance(trace, Mapping)
    left = trace["left"]
    assert isinstance(left, Mapping)
    assert left["indicator"] == "rsi"
    assert float(left["value"]) == pytest.approx(100.0)


def test_builtin_factory_strategy_runs_through_the_real_event_engine() -> None:
    from qt.backtest.strategy_backtest import _market_dataset, _spec, synthetic_btc_ohlcv

    frame = synthetic_btc_ohlcv(days=2)
    built = build_lab_strategy(_builtin_version("buy_and_hold"))
    spec = _spec(built.strategy.metadata.id, frame, initial_cash=10_000, synthetic=True)
    market = _market_dataset(
        "spot",
        frame,
        request=spec.data,
        real_data=False,
        source="strategy-factory-test",
    )
    result = EventRunner().run(
        spec,
        MarketBundle(primary=market, auxiliary={}),
        built.strategy,
    )
    assert result.strategy_id == "buy_and_hold"
    assert len(result.fills) == 1
