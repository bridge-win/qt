"""Real installed Nautilus runs, immutable V2 inputs and causal prefix checks."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from qt.contracts import (
    ExperimentSpecV2,
    FeeSchedule,
    InstrumentSpec,
    StrategyArtifact,
    content_hash,
)
from qt.datasets import DatasetRegistry
from qt.experiments.native import builtin_strategy_code_hash, run

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("nautilus_trader") is None
    or importlib.metadata.version("nautilus_trader") != "2.0.0rc4",
    reason="V2 production experiment baseline remains pinned to rc4; candidate refusal has a separate test",
)


def native_input(tmp_path, *, interval=900_000_000_000, periods=100, test_periods=80,
                 suffix="data", future_price=0):
    base = pd.Timestamp("2020-01-01", tz="UTC").value
    close = 100 + 5 * np.sin(np.arange(periods) / 3) + np.arange(periods) * 0.03
    close[test_periods:] += future_price
    registry = DatasetRegistry(tmp_path / suffix)
    table = pa.table({
        "timestamp_ns": [base + i * interval for i in range(periods)],
        "open": close, "high": close + 1, "low": close - 1,
        "close": close, "volume": [100.0] * periods,
    })
    manifest = registry.register_table(
        table, venue="binance", instrument="BTCUSDT", data_type="ohlcv",
        timestamp_column="timestamp_ns", rights="synthetic_test_only",
        provenance={"timestamp_semantics": "open", "source": "frozen_synthetic"},
        availability="reconstructed_available", expected_interval_ns=interval,
    )
    instrument = InstrumentSpec(
        venue="binance", instrument="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        settlement_asset="USDT", price_increment="0.01", quantity_increment="0.00001",
        minimum_quantity="0.00001", minimum_notional="1", effective_from_ns=0,
        source="frozen_fixture",
    )
    fee = FeeSchedule(
        venue="binance", account_tier="fixture", maker_rate="0.001", taker_rate="0.001",
        fee_basis="notional", fee_asset="USDT", effective_from_ns=0,
        source="frozen_fixture", source_hash="a" * 64,
    )
    strategy = StrategyArtifact.model_validate({
        "strategy_id": "sma_crossover", "version": "v1",
        "code_hash": builtin_strategy_code_hash("sma_crossover"),
        "parameters": {"kind": "sma_crossover", "fast": 3, "slow": 8},
        "feature_dependencies": ("close",), "instruments": ("BTCUSDT",),
        "signal_interval_ns": interval, "signal_ttl_ns": interval,
        "exit_rules": {}, "risk_budget": {"capital": "10000", "currency": "USDT"},
        "engine_compatibility": ("nautilus",),
    })
    spec = ExperimentSpecV2.model_validate({
        "campaign_id": "campaign", "strategy_version": content_hash(strategy),
        "dataset_ids": (manifest.dataset_id,), "instruments": ("BTCUSDT",),
        "timeframes_ns": (interval,),
        "split": {"method": "walk_forward", "training_start_ns": base,
                  "training_end_ns": base + 10 * interval,
                  "validation_end_ns": base + 20 * interval,
                  "test_end_ns": base + test_periods * interval,
                  "label_information_interval": "closed_bar_to_next_closed_bar"},
        "cost_model": {"version": "frozen-v1", "fee_schedule_hashes": (content_hash(fee),), "slippage_bps": "0"},
        "fill_model": {"name": "native_bar", "version": "rc4-v1"},
        "latency_model": {"version": "static-v1", "feed_ns": 0, "decision_ns": 0, "submission_ns": 0, "exchange_ns": 0},
        "benchmarks": ("cash",), "seed": 7,
        "engine": {"name": "nautilus", "version": "2.0.0rc4", "image_digest": "sha256:" + "b" * 64},
        "policy_version": "research-v1",
    })
    return spec, strategy, registry, [instrument], [fee]


@pytest.mark.parametrize("interval", [900_000_000_000, 3_600_000_000_000])
def test_v2_uses_real_native_engine_at_fifteen_minutes_and_hour(tmp_path, interval):
    inputs = native_input(tmp_path, interval=interval)
    raw = run(*inputs)
    assert raw["native_version"] == "2.0.0rc4"
    assert raw["result"]["account_invariants"]["status"] == "verified"
    assert raw["result"]["realized_costs"]["fees_by_currency"]["USDT"] != "0"
    assert len(raw["result"]["nav"]) == 61
    assert raw["result"]["fills"]
    assert raw["result"]["decision_traces"][0]["observed_rows"] == 21
    assert raw["result"]["metrics"]["periods_per_year"] == 365
    assert raw["quality_flags"] == ["RECONSTRUCTED_RECEIPT"]
    assert raw["live_ready"] is False
    payload = {key: value for key, value in raw.items() if key != "sha256"}
    assert content_hash(payload) == raw["sha256"]
    json.dumps(raw, allow_nan=False)


def test_native_result_ignores_all_prices_after_frozen_test_window(tmp_path):
    full = run(*native_input(tmp_path, suffix="full"))
    truncated = run(*native_input(tmp_path, suffix="prefix", periods=80))
    poisoned = run(*native_input(tmp_path, suffix="poisoned", future_price=1_000_000))
    assert full["result"]["nav"] == truncated["result"]["nav"] == poisoned["result"]["nav"]
    assert full["result"]["decision_traces"] == truncated["result"]["decision_traces"] == poisoned["result"]["decision_traces"]
    assert full["result"]["account_invariants"] == truncated["result"]["account_invariants"]


def test_native_does_not_keep_old_two_thousand_bar_limit(tmp_path):
    raw = run(*native_input(tmp_path, periods=2020, test_periods=2010))
    assert len(raw["result"]["decision_traces"]) == 1990
    assert raw["window"]["warmup_bars"] == 20


def test_native_rejects_unimplemented_costs_versions_and_shared_sleeves(tmp_path):
    inputs = native_input(tmp_path)
    spec, strategy, registry, instruments, fees = inputs
    body = spec.model_dump(mode="json")
    body["cost_model"]["slippage_bps"] = "5"
    with pytest.raises(ValueError, match="fixed_bps_slippage"):
        run(ExperimentSpecV2.model_validate_json(json.dumps(body)), strategy, registry, instruments, fees)
    body = spec.model_dump(mode="json")
    body["engine"]["version"] = "2.0.0rc7"
    with pytest.raises(ValueError, match="version_mismatch"):
        run(ExperimentSpecV2.model_validate_json(json.dumps(body)), strategy, registry, instruments, fees)
    body = spec.model_dump(mode="json")
    body["instruments"].append("ETHUSDT")
    with pytest.raises(ValueError, match="one_instrument"):
        run(ExperimentSpecV2.model_validate_json(json.dumps(body)), strategy, registry, instruments, fees)


def test_native_declared_image_and_strategy_code_must_match(tmp_path, monkeypatch):
    inputs = native_input(tmp_path)
    monkeypatch.setenv("QT_EXPERIMENT_IMAGE_DIGEST", "sha256:wrong-image")
    with pytest.raises(ValueError, match="image_digest_mismatch"):
        run(*inputs)
    monkeypatch.delenv("QT_EXPERIMENT_IMAGE_DIGEST")
    spec, strategy, registry, instruments, fees = inputs
    wrong = StrategyArtifact.model_validate({**strategy.model_dump(mode="python"), "code_hash": "a" * 64})
    spec = ExperimentSpecV2.model_validate({**spec.model_dump(mode="python"), "strategy_version": content_hash(wrong)})
    with pytest.raises(ValueError, match="code_hash_mismatch"):
        run(spec, wrong, registry, instruments, fees)


def test_native_checks_cancellation_before_start(tmp_path):
    with pytest.raises(InterruptedError, match="cancelled"):
        run(*native_input(tmp_path), cancelled=lambda: True)


def test_native_binds_instrument_version_when_hashes_are_declared(tmp_path):
    spec, strategy, registry, instruments, fees = native_input(tmp_path)
    changed = ExperimentSpecV2.model_validate({
        **spec.model_dump(mode="python"), "instrument_spec_hashes": ("a" * 64,),
    })
    with pytest.raises(ValueError, match="instrument_version_hash_mismatch"):
        run(changed, strategy, registry, instruments, fees)


def test_shared_native_sizing_slices_protective_sell_to_same_order_budget(tmp_path):
    from decimal import Decimal
    from types import SimpleNamespace

    from btc_backtest.engine.models import InstrumentKind, OrderSide, OrderType
    from btc_backtest.strategies.base import OrderIntent
    from btc_backtest.strategies.registry import default_strategy_registry

    from qt.experiments.native import _BudgetedBuiltin

    _, artifact, _, instruments, fees = native_input(tmp_path)
    source = default_strategy_registry().create("sma_crossover", {"fast_window": 3, "slow_window": 8})
    requested = OrderIntent(instrument=InstrumentKind.SPOT, side=OrderSide.SELL,
                            order_type=OrderType.MARKET, base_quantity=Decimal("30"), reason="exit")
    source.on_bar = lambda _context: (requested,)
    context = SimpleNamespace(
        current_bar={"close": Decimal("100")}, timestamp=pd.Timestamp("2020-01-01", tz="UTC"),
        portfolio=SimpleNamespace(cash=Decimal("7000"),
                                  position=lambda _: SimpleNamespace(quantity=Decimal("30"))),
    )
    bounded = _BudgetedBuiltin(source, artifact, instruments[0], fees[0].taker_rate)
    first, = bounded.on_bar(context)
    assert first.base_quantity == Decimal("2")
    assert requested.base_quantity == Decimal("30")
    # The next closed-bar decision can continue the same bounded exit. Native
    # and Paper use this wrapper, so a full accumulated position never needs a
    # single oversized order that the production OMS must reject.
    context.portfolio.position = lambda _: SimpleNamespace(quantity=Decimal("28"))
    second, = bounded.on_bar(context)
    assert second.base_quantity == Decimal("2")
