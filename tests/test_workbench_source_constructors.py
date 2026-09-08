from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qt.strategy_ports.btcqt import (
    BtcqtCausalRecord,
    BtcqtNativeStrategy,
    causal_records_fingerprint,
)
from qt.strategy_ports.freqtrade import FreqtradeNativeStrategy, MissingFreqtradeDataError
from qt.workbench.catalog import legacy_catalog
from qt.workbench.source_constructors import build_source_strategy


def _version(identity: str, source_version: str) -> dict[str, object]:
    return {
        "version_id": "source-v1",
        "content": {
            "mode": "builtin",
            "builtin_identity": identity,
            "builtin_version": source_version,
            "builtin_parameters": {},
        },
    }


def test_catalog_constructor_builds_source_native_4h_strategy() -> None:
    profile = legacy_catalog()["profiles"][0]
    assert isinstance(profile, dict)
    strategy = build_source_strategy(
        _version(f"catalog:{profile['id']}", str(profile.get("version", 1))),
        {},
        dataset_version="sha256:dataset-v1",
    )

    assert isinstance(strategy, FreqtradeNativeStrategy)
    assert strategy.metadata.supported_timeframes == ("4h",)


def test_btcqt_constructor_requires_one_minute_perpetual_native_semantics() -> None:
    from qt.strategy_ports.btcqt import BTCQT_PORTS

    strategy = build_source_strategy(
        _version("btcqt:btcqt_s0_trend", BTCQT_PORTS["btcqt_s0_trend"].source.commit),
        {},
        dataset_version="sha256:btcqt-1m",
        leverage=Decimal("2"),
    )

    assert isinstance(strategy, BtcqtNativeStrategy)
    assert strategy.metadata.supported_timeframes == ("1m",)
    assert strategy.native_instrument_kind.value == "perpetual"
    with pytest.raises(ValueError, match="1x-2x"):
        build_source_strategy(
            _version("btcqt:btcqt_s0_trend", BTCQT_PORTS["btcqt_s0_trend"].source.commit),
            {},
            dataset_version="sha256:btcqt-1m",
            leverage=Decimal("3"),
        )


def test_btcqt_auxiliary_records_are_content_attested_and_revisions_stay_causal() -> None:
    from qt.strategy_ports.btcqt import BTCQT_PORTS

    start = datetime(2025, 1, 1, tzinfo=timezone.utc)

    def dataset(dataset_id: str, records: list[dict[str, object]]) -> dict[str, object]:
        parsed = [
            BtcqtCausalRecord(
                observed_at=datetime.fromisoformat(str(record["observed_at"])),
                available_at=datetime.fromisoformat(str(record["available_at"])),
                values={key: value for key, value in record.items() if key not in {"observed_at", "available_at"}},
            )
            for record in records
        ]
        return {
            "dataset_id": dataset_id,
            "fingerprint": causal_records_fingerprint(dataset_id, parsed),
            "records": records,
        }

    funding = [
        {"observed_at": start.isoformat(), "available_at": start.isoformat(), "rate": 0.001},
    ]
    oi = [
        {"observed_at": start.isoformat(), "available_at": start.isoformat(), "oi": 100.0},
        {
            "observed_at": start.isoformat(),
            "available_at": (start + timedelta(minutes=2)).isoformat(),
            "oi": 200.0,
        },
    ]
    bundle = {
        "funding_settlements": dataset("funding_settlements", funding),
        "open_interest": dataset("open_interest", oi),
    }
    strategy = build_source_strategy(
        _version("btcqt:btcqt_s2_crowding_fader", BTCQT_PORTS["btcqt_s2_crowding_fader"].source.commit),
        {"enabled": True},
        dataset_version="sha256:btcqt-1m",
        causal_bundle=bundle,
    )
    assert isinstance(strategy, BtcqtNativeStrategy)
    index = pd.date_range(start, periods=2, freq="1min")
    bars = pd.DataFrame(
        {"open": [100.0, 100.0], "high": [101.0, 101.0], "low": [99.0, 99.0], "close": [100.0, 100.0], "volume": [1.0, 1.0]},
        index=index,
    )
    first = strategy.native_decision(timestamp=index[0].to_pydatetime(), bars=bars, equity=Decimal("1000"))
    second = strategy.native_decision(timestamp=index[1].to_pydatetime(), bars=bars, equity=Decimal("1000"))
    assert first.state.state.open_interest == 100.0
    assert second.state.state.open_interest == 200.0

    tampered = {**bundle, "open_interest": {**bundle["open_interest"], "records": [*oi[:-1], {**oi[-1], "oi": 999.0}]}}
    with pytest.raises(ValueError, match="fingerprint does not match"):
        build_source_strategy(
            _version("btcqt:btcqt_s2_crowding_fader", BTCQT_PORTS["btcqt_s2_crowding_fader"].source.commit),
            {"enabled": True},
            dataset_version="sha256:btcqt-1m",
            causal_bundle=tampered,
        )


def test_btcqt_lab_job_is_fail_closed_until_perpetual_acceptance(tmp_path: Path) -> None:
    pytest.importorskip("nautilus_trader")

    from qt.nautilus.executor import NautilusResearchExecutor
    from qt.strategy_ports.btcqt import BTCQT_PORTS

    root = tmp_path / "parquet"
    directory = root / "ohlcv"
    directory.mkdir(parents=True)
    index = pd.date_range("2025-01-01", periods=302, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": np.full(len(index), 100.0),
            "high": np.full(len(index), 101.0),
            "low": np.full(len(index), 99.0),
            "close": np.full(len(index), 100.0),
            "volume": np.full(len(index), 10.0),
        },
        index=index,
    )
    path = directory / "binance_BTCUSDT_1m.parquet"
    frame.to_parquet(path)
    path.with_suffix(".manifest.json").write_text(
        '{"provider":"binanceusdm","symbol":"BTC/USDT","timeframe":"1m","market":"perpetual"}',
        encoding="utf-8",
    )
    executor = NautilusResearchExecutor(root, tmp_path / "artifacts")
    dataset = executor.datasets.get("binance-btcusdt-1m")
    with pytest.raises(ValueError, match="perpetual and leveraged research are disabled"):
        executor.execute(
        {
            "mode": "lab_strategy_version",
            "lab_strategy_version": _version(
                "btcqt:btcqt_s1_wick_ladder",
                BTCQT_PORTS["btcqt_s1_wick_ladder"].source.commit,
            ),
            "dataset_id": "binance-btcusdt-1m",
            "dataset_fingerprint": dataset["fingerprint"],
            "market": "perpetual",
            "assumptions": {"initial_cash": 10_000, "fee_bps": 10, "leverage": 2},
            "seed": 7,
        },
        lambda stage, percent: None,
        lambda: False,
        )


def test_catalog_strategy_runs_through_native_freqtrade_router_on_4h_bars() -> None:
    pytest.importorskip("nautilus_trader")
    from qt.nautilus.models import ExecutionAssumptions
    from qt.nautilus.runner import NautilusDataFrameRunner

    profile = legacy_catalog()["profiles"][0]
    assert isinstance(profile, dict)
    strategy = build_source_strategy(
        _version(f"catalog:{profile['id']}", str(profile.get("version", 1))),
        {},
        dataset_version="sha256:catalog-4h-fixture",
    )
    index = pd.date_range("2024-01-01", periods=300, freq="4h", tz="UTC")
    close = 100 + np.sin(np.linspace(0, 12, len(index))) * 5 + np.linspace(0, 3, len(index))
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.full(len(index), 10.0),
        },
        index=index,
    )

    run = NautilusDataFrameRunner().run(
        frame=frame,
        strategy=strategy,
        assumptions=ExecutionAssumptions(
            initial_cash=Decimal("10000"),
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0"),
        ),
    )

    assert len(run.traces) == len(frame)
    assert all(
        dict(trace.observed_values)["source_strategy_id"] == "btc_quant_catalog"
        for trace in run.traces
    )
    assert run.account_invariants["status"] == "verified"


def test_catalog_profile_native_entry_exit_uses_source_timing_and_real_fills() -> None:
    pytest.importorskip("nautilus_trader")
    from qt.nautilus.models import ExecutionAssumptions
    from qt.nautilus.runner import NautilusDataFrameRunner

    profile = next(
        item
        for item in legacy_catalog()["profiles"]
        if isinstance(item, dict) and item["id"] == "macd-momentum-v01"
    )
    strategy = build_source_strategy(
        _version(f"catalog:{profile['id']}", str(profile.get("version", 1))),
        {},
        dataset_version="sha256:catalog-lifecycle-fixture",
    )
    close = np.concatenate(
        (
            np.full(260, 100.0),
            np.linspace(70, 95, 16),
            np.linspace(96, 140, 30),
            np.linspace(140, 110, 20),
        )
    )
    index = pd.date_range("2024-01-01", periods=len(close), freq="4h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.full(len(index), 10.0),
        },
        index=index,
    )

    run = NautilusDataFrameRunner().run(
        frame=frame,
        strategy=strategy,
        assumptions=ExecutionAssumptions(
            initial_cash=Decimal("10000"),
            maker_fee=Decimal("0.001"),
            taker_fee=Decimal("0.001"),
        ),
    )

    source_traces = [trace for trace in run.traces if trace.intent_count]
    assert source_traces[0].timestamp == index[270].to_pydatetime()
    assert dict(source_traces[0].observed_values)["source_orders"] == ("enter_long",)
    assert any(
        dict(trace.observed_values)["source_orders"] == ("exit_long",)
        for trace in source_traces[1:]
    )
    assert len(run.orders) >= 2
    assert len(run.fills) >= 2
    assert run.metrics["costs"]["fees"] != "0"
    assert run.account_invariants["status"] == "verified"
    assert run.account_invariants["native_fill_transactions"] >= 2


def test_multisource_constructor_rejects_missing_causal_feature_matrix() -> None:
    from qt.strategy_ports.freqtrade import FREQTRADE_PORTS

    with pytest.raises(MissingFreqtradeDataError, match="versioned external inputs"):
        build_source_strategy(
            _version(
                "freqtrade:btc_multisource_regime",
                FREQTRADE_PORTS["btc_multisource_regime"].source.commit,
            ),
            {},
            dataset_version="sha256:dataset-v1",
        )


def test_qt5_workflow_constructor_refuses_order_strategy_coercion() -> None:
    with pytest.raises(ValueError, match="legacy_workflows"):
        build_source_strategy(
            _version("qt5live:qt5_wick_catcher", "qt5-gallery"),
            {},
            dataset_version="sha256:dataset-v1",
        )
