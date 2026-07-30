from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from qt.backtest.strategy_backtest import synthetic_btc_ohlcv
from qt.data.store import ParquetStore
from qt.research.executor import ResearchExecutor


def _spec(profile: str = "quick") -> dict[str, object]:
    return {
        "dataset_id": "okx-btcusdt-1h",
        "ohlcv_key": "okx_BTCUSDT_1h",
        "mode": "template",
        "strategy_id": "buy_and_hold",
        "strategy_params": {},
        "validation_profile": profile,
        "assumptions": {
            "initial_cash": 10_000,
            "fee_bps": 10,
            "slippage_bps": 5,
        },
        "seed": 7,
    }


def test_quick_executor_writes_reproducible_result_and_benchmarks(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "parquet"
    backtests = tmp_path / "backtests"
    ParquetStore(parquet).write(
        "ohlcv",
        "okx_BTCUSDT_1h",
        synthetic_btc_ohlcv(days=45),
    )
    stages: list[str] = []
    executor = ResearchExecutor(parquet, backtests)

    result = executor.execute(
        _spec(),
        lambda stage, progress: stages.append(f"{stage}:{progress}"),
        lambda: False,
    )

    assert result["run_id"]
    assert result["strategy"] == "buy_and_hold"
    assert result["validation_profile"] == "quick"
    assert result["benchmarks"]["buy_and_hold"]["metrics"]
    assert result["benchmarks"]["fixed_dca"]["metrics"]
    assert result["data"]["fingerprint"]
    assert result["verdict"]["live_ready"] is False
    assert result["artifacts"] == [
        "data_manifest.json",
        "equity.csv",
        "summary.json",
        "trades.csv",
    ]
    run_dir = backtests / str(result["run_id"])
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "data_manifest.json").exists()
    trades = pd.read_csv(run_dir / "trades.csv")
    assert {"reason", "holding_hours"}.issubset(trades.columns)
    assert "simulation:35" in stages


def test_standard_executor_requires_ready_ten_year_standard(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "parquet"
    ParquetStore(parquet).write(
        "ohlcv",
        "okx_BTCUSDT_1h",
        synthetic_btc_ohlcv(days=45),
    )
    executor = ResearchExecutor(parquet, tmp_path / "backtests")

    with pytest.raises(ValueError, match="10-year Bitstamp"):
        executor.execute(
            _spec("standard"),
            lambda stage, progress: None,
            lambda: False,
        )


def test_future_candles_do_not_change_prior_buy_and_hold_fills(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "parquet"
    frame = synthetic_btc_ohlcv(days=60)
    ParquetStore(parquet).write("ohlcv", "okx_BTCUSDT_1h", frame)
    executor = ResearchExecutor(parquet, tmp_path / "backtests")

    assert executor._future_data_consistent(
        _spec(),
        frame.iloc[: 45 * 24],
        frame,
    )


def test_buy_and_hold_returns_are_stable_across_warmup_lengths(
    tmp_path: Path,
) -> None:
    parquet = tmp_path / "parquet"
    frame = synthetic_btc_ohlcv(days=60)
    ParquetStore(parquet).write("ohlcv", "okx_BTCUSDT_1h", frame)
    executor = ResearchExecutor(parquet, tmp_path / "backtests")

    assert executor._recursive_stable(_spec(), frame)
