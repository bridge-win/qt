from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from btc_backtest.engine.models import OrderIntent
from btc_backtest.errors import StrategyLoadError
from btc_backtest.strategies import loader
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.loader import (
    discover_entry_point_strategies,
    load_strategy,
)

PACKAGE_ROOT = Path(__file__).parents[2]


class EntryPointStrategy:
    metadata = StrategyMetadata(
        id="external_fixture",
        version="1.0.0",
        api_version="1",
        description="Fixture strategy",
        warmup_bars=0,
        supported_timeframes=("1d",),
    )

    def initialize(self, context: InitializationContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        return ()

    def finalize(self, context: FinalizationContext) -> None:
        return None


class DuplicateEntryPointStrategy(EntryPointStrategy):
    metadata = EntryPointStrategy.metadata.model_copy()


class FakeEntryPoint:
    def __init__(
        self,
        name: str,
        factory: Callable[[], object],
    ) -> None:
        self.name = name
        self._factory = factory

    def load(self) -> object:
        return self._factory


def test_load_explicit_custom_strategy() -> None:
    reference = (
        PACKAGE_ROOT / "examples" / "custom_strategy.py"
    ).as_posix() + ":CustomStrategy"

    strategy = load_strategy(reference)

    assert strategy.metadata.id == "custom_sma"
    assert strategy.metadata.api_version == "1"


def test_load_explicit_fixture_plugin() -> None:
    reference = (
        PACKAGE_ROOT
        / "tests"
        / "fixtures"
        / "custom-plugin"
        / "src"
        / "example_btc_strategy"
        / "__init__.py"
    ).as_posix() + ":ExternalFixtureStrategy"

    strategy = load_strategy(reference)

    assert strategy.metadata.id == "external_fixture"


def test_duplicate_entry_point_ids_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        FakeEntryPoint("first", EntryPointStrategy),
        FakeEntryPoint("second", DuplicateEntryPointStrategy),
    ]
    monkeypatch.setattr(loader, "entry_points", lambda **_: entries)

    with pytest.raises(StrategyLoadError, match="duplicate"):
        discover_entry_point_strategies()


def test_entry_point_group_is_discoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [FakeEntryPoint("fixture", EntryPointStrategy)]
    monkeypatch.setattr(loader, "entry_points", lambda **_: entries)

    discovered = discover_entry_point_strategies()

    assert discovered["external_fixture"].metadata.api_version == "1"


def test_explicit_loader_rejects_missing_protocol_method(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.py"
    invalid.write_text(
        "\n".join(
            [
                "from btc_backtest.strategies.base import StrategyMetadata",
                "class InvalidStrategy:",
                "    metadata = StrategyMetadata(",
                "        id='invalid', version='1', api_version='1',",
                "        description='invalid', warmup_bars=0,",
                "        supported_timeframes=('1d',),",
                "    )",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(StrategyLoadError, match="protocol"):
        load_strategy(f"{invalid}:InvalidStrategy")


def test_explicit_loader_requires_exact_file_and_class_reference() -> None:
    with pytest.raises(StrategyLoadError, match=r"file\.py:ClassName"):
        load_strategy("btc_backtest.strategies")
