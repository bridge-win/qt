"""QT legacy façade compatibility over the independent btc_backtest engine."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from qt.backtest.strategy_backtest import (
    canonical_strategy,
    run_strategy_backtest,
    synthetic_btc_ohlcv,
    write_strategy_backtest_artifacts,
)


@pytest.fixture()
def fixture_ohlcv() -> pd.DataFrame:
    return synthetic_btc_ohlcv(days=260, freq="1d", seed=31)


@pytest.mark.parametrize(
    ("legacy", "new"),
    [
        ("dca", "smart_dca"),
        ("trend", "sma_crossover"),
        ("carry", "funding_basis_carry"),
        ("wick", "wick_catcher"),
    ],
)
def test_legacy_alias_executes_new_engine(
    legacy: str,
    new: str,
    fixture_ohlcv: pd.DataFrame,
) -> None:
    outcome = run_strategy_backtest(
        legacy,
        fixture_ohlcv,
        allow_synthetic=False,
    )

    assert outcome.strategy == legacy
    assert outcome.engine_strategy == new
    assert outcome.synthetic is False
    assert outcome.data_fingerprint
    assert len(outcome.equity) == len(fixture_ohlcv)


@pytest.mark.parametrize(
    ("alias", "legacy"),
    [
        ("smart_dca", "dca"),
        ("sma_crossover", "trend"),
        ("funding_basis_carry", "carry"),
        ("wick_catcher", "wick"),
    ],
)
def test_engine_aliases_keep_legacy_qt_ids(alias: str, legacy: str) -> None:
    assert canonical_strategy(alias) == legacy


def test_legacy_artifact_summary_keeps_required_fields(
    tmp_path: Path,
    fixture_ohlcv: pd.DataFrame,
) -> None:
    path = write_strategy_backtest_artifacts(
        run_strategy_backtest("trend", fixture_ohlcv, allow_synthetic=False),
        tmp_path,
    )

    summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
    assert {
        "strategy",
        "engine_strategy",
        "synthetic",
        "bars",
        "metrics",
        "data_fingerprint",
    } <= summary.keys()
    assert summary["strategy"] == "trend"
    assert summary["engine_strategy"] == "sma_crossover"
