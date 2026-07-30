from __future__ import annotations

from pathlib import Path

import pytest
from btc_backtest.strategies.ensemble import WeightedEnsemble

from qt.backtest.strategy_backtest import synthetic_btc_ohlcv
from qt.data.store import ParquetStore
from qt.research.datasets import DatasetCatalog
from qt.research.service import build_strategy, normalize_job_request


def _catalog(tmp_path: Path) -> DatasetCatalog:
    store = ParquetStore(tmp_path)
    store.write("ohlcv", "okx_BTCUSDT_1h", synthetic_btc_ohlcv(days=45))
    return DatasetCatalog(tmp_path)


def test_normalize_template_request_keeps_real_strategy_identity(
    tmp_path: Path,
) -> None:
    normalized = normalize_job_request(
        {
            "dataset_id": "okx-btcusdt-1h",
            "mode": "template",
            "template": {
                "strategy_id": "sma_crossover",
                "parameters": {"fast_window": 10, "slow_window": 30},
            },
            "validation_profile": "quick",
        },
        _catalog(tmp_path),
    )

    assert normalized["strategy_id"] == "sma_crossover"
    assert normalized["strategy_params"] == {
        "fast_window": 10,
        "slow_window": 30,
    }
    assert normalized["mode"] == "template"


def test_normalize_template_rejects_custom_rules(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="custom_rules"):
        normalize_job_request(
            {
                "dataset_id": "okx-btcusdt-1h",
                "mode": "template",
                "template": {"strategy_id": "sma_crossover", "parameters": {}},
                "rules": {"entry": {"conditions": [{"indicator": "rsi_below"}]}},
            },
            _catalog(tmp_path),
        )

def test_normalize_custom_rules_uses_explicit_strategy_identity(
    tmp_path: Path,
) -> None:
    normalized = normalize_job_request(
        {
            "dataset_id": "okx-btcusdt-1h",
            "mode": "custom_rules",
            "rules": {
                "entry": {
                    "operator": "ALL",
                    "conditions": [
                        {"indicator": "rsi_below", "window": 14, "threshold": 30}
                    ],
                },
                "exit": {
                    "operator": "ANY",
                    "conditions": [
                        {"indicator": "rsi_above", "window": 14, "threshold": 55}
                    ],
                },
            },
        },
        _catalog(tmp_path),
    )

    assert normalized["strategy_id"] == "custom_rule_recipe"
    assert normalized["mode"] == "custom_rules"


def test_build_strategy_creates_weighted_target_ensemble(tmp_path: Path) -> None:
    normalized = normalize_job_request(
        {
            "dataset_id": "okx-btcusdt-1h",
            "mode": "ensemble",
            "ensemble": {
                "components": [
                    {
                        "strategy_id": "sma_crossover",
                        "parameters": {"fast_window": 10, "slow_window": 30},
                        "weight": 3,
                    },
                    {
                        "strategy_id": "rsi_mean_reversion",
                        "parameters": {},
                        "weight": 1,
                    },
                ]
            },
        },
        _catalog(tmp_path),
    )

    strategy = build_strategy(normalized)

    assert isinstance(strategy, WeightedEnsemble)
    assert strategy.component_weights == (pytest.approx(0.75), pytest.approx(0.25))


def test_ensemble_rejects_non_target_weight_strategy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not ensemble-compatible"):
        normalize_job_request(
            {
                "dataset_id": "okx-btcusdt-1h",
                "mode": "ensemble",
                "ensemble": {
                    "components": [
                        {"strategy_id": "fixed_dca", "parameters": {}, "weight": 1},
                        {"strategy_id": "sma_crossover", "parameters": {}, "weight": 1},
                    ]
                },
            },
            _catalog(tmp_path),
        )
