from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from btc_backtest.engine.models import OrderIntent
from btc_backtest.errors import StrategyLoadError
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    Strategy,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.registry import (
    BUILTIN_STRATEGY_IDS,
    StrategyRegistry,
)

EXPECTED = (
    "fixed_dca",
    "smart_dca",
    "sma_crossover",
    "ema_crossover",
    "macd_trend",
    "rsi_mean_reversion",
    "stochastic_reversal",
    "bollinger_mean_reversion",
    "bollinger_breakout",
    "donchian_breakout",
    "turtle_trend",
    "time_series_momentum",
    "dual_momentum",
    "rate_of_change",
    "adx_trend",
    "atr_volatility_breakout",
    "keltner_channel",
    "vwap_mean_reversion",
    "grid_rebalance",
    "funding_basis_carry",
)


class RegistryFixtureStrategy:
    metadata = StrategyMetadata(
        id="registry_fixture",
        version="1.0.0",
        description="Registry fixture.",
        warmup_bars=0,
        supported_timeframes=("1d",),
        parameter_schema={"window": {"type": "integer", "minimum": 1}},
    )

    def __init__(self, parameters: Mapping[str, object]) -> None:
        self.parameters = dict(parameters)

    def initialize(self, context: InitializationContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]:
        return ()

    def finalize(self, context: FinalizationContext) -> None:
        return None


def _factory(parameters: Mapping[str, object]) -> Strategy:
    return RegistryFixtureStrategy(parameters)


def test_exact_top_twenty_catalog() -> None:
    assert BUILTIN_STRATEGY_IDS == EXPECTED
    assert len(set(BUILTIN_STRATEGY_IDS)) == 20


def test_unregistered_builtin_fails_clearly() -> None:
    with pytest.raises(
        StrategyLoadError,
        match="strategy implementation not registered: sma_crossover",
    ):
        StrategyRegistry().create("sma_crossover", {})


def test_registered_factory_create_list_and_describe() -> None:
    registry = StrategyRegistry()
    registry.register(
        "registry_fixture",
        _factory,
        RegistryFixtureStrategy.metadata,
    )

    created = registry.create("registry_fixture", {"window": 7})

    assert isinstance(created, RegistryFixtureStrategy)
    assert created.parameters == {"window": 7}
    assert registry.list() == ("registry_fixture",)
    assert registry.describe("registry_fixture") == created.metadata
    assert registry.strategies == {"registry_fixture": _factory}


def test_factory_receives_an_isolated_immutable_parameter_mapping() -> None:
    captured: list[Mapping[str, object]] = []

    def capture(parameters: Mapping[str, object]) -> Strategy:
        captured.append(parameters)
        return RegistryFixtureStrategy(parameters)

    registry = StrategyRegistry()
    registry.register(
        "registry_fixture",
        capture,
        RegistryFixtureStrategy.metadata,
    )
    source = {"window": 5}

    registry.create("registry_fixture", source)
    source["window"] = 99

    assert captured[0]["window"] == 5
    with pytest.raises(TypeError):
        captured[0]["window"] = 10  # type: ignore[index]


def test_duplicate_registration_and_metadata_mismatch_fail() -> None:
    registry = StrategyRegistry()
    registry.register(
        "registry_fixture",
        _factory,
        RegistryFixtureStrategy.metadata,
    )

    with pytest.raises(StrategyLoadError, match="duplicate"):
        registry.register(
            "registry_fixture",
            _factory,
            RegistryFixtureStrategy.metadata,
        )
    with pytest.raises(StrategyLoadError, match="metadata id"):
        StrategyRegistry().register(
            "different",
            _factory,
            RegistryFixtureStrategy.metadata,
        )


def test_alias_resolves_without_changing_catalog_or_list() -> None:
    registry = StrategyRegistry()
    registry.register(
        "registry_fixture",
        _factory,
        RegistryFixtureStrategy.metadata,
    )
    registry.register_alias("fixture_alias", "registry_fixture")

    created = registry.create("fixture_alias", {})

    assert created.metadata.id == "registry_fixture"
    assert registry.list() == ("registry_fixture",)
    assert BUILTIN_STRATEGY_IDS == EXPECTED
    with pytest.raises(StrategyLoadError, match="duplicate"):
        registry.register_alias("fixture_alias", "registry_fixture")


def test_unknown_strategy_and_alias_target_fail_clearly() -> None:
    registry = StrategyRegistry()

    with pytest.raises(StrategyLoadError, match="unknown strategy"):
        registry.create("not_real", {})
    with pytest.raises(StrategyLoadError, match="unknown alias target"):
        registry.register_alias("alias", "not_real")
