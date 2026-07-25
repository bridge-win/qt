from __future__ import annotations

import json
from pathlib import Path

import pytest
from btc_backtest.strategies.registry import (
    BUILTIN_STRATEGY_IDS,
    EXTRA_STRATEGY_IDS,
    default_strategy_registry,
)

from .catalog_support import canonical_summary, run_catalog

GOLDEN_PATH = Path(__file__).with_name("golden") / "catalog-v1.json"
ALL_STRATEGY_IDS = BUILTIN_STRATEGY_IDS + EXTRA_STRATEGY_IDS


@pytest.mark.parametrize("strategy_id", ALL_STRATEGY_IDS)
def test_strategy_matches_reviewed_golden(strategy_id: str) -> None:
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    result, bundle = run_catalog(strategy_id)

    assert canonical_summary(result, bundle) == expected[strategy_id]


def test_golden_fixture_covers_exact_registered_catalog() -> None:
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    assert tuple(expected) == ALL_STRATEGY_IDS
    assert default_strategy_registry().list() == ALL_STRATEGY_IDS


@pytest.mark.parametrize("strategy_id", ALL_STRATEGY_IDS)
def test_every_strategy_is_constructible_and_metadata_aligned(
    strategy_id: str,
) -> None:
    registry = default_strategy_registry()

    strategy = registry.create(strategy_id, {})

    assert strategy.metadata.id == strategy_id
    assert strategy.metadata.parameter_schema
