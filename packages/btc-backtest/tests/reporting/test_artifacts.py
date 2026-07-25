from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
from btc_backtest.data.models import DataManifest
from btc_backtest.engine.models import (
    BacktestResult,
    InstrumentKind,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.reporting.artifacts import ArtifactWriter
from btc_backtest.reporting.metrics import PerformanceMetrics
from btc_backtest.validation.models import ValidationResult, ValidationSpec

UTC = timezone.utc

EXPECTED_FILES = {
    "run.json",
    "data_manifest.json",
    "equity.parquet",
    "positions.parquet",
    "orders.parquet",
    "fills.parquet",
    "trades.parquet",
    "signals.parquet",
    "metrics.json",
    "validation.json",
    "report.html",
}


def test_writer_creates_complete_attributed_bundle(tmp_path: Path) -> None:
    result = complete_result()

    bundle = ArtifactWriter().write(
        result,
        metrics(),
        validation_result(),
        tmp_path,
    )

    assert {path.name for path in bundle.run_dir.iterdir()} == EXPECTED_FILES
    run = json.loads((bundle.run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["data_fingerprint"] == result.data_manifests[0].normalized_sha256
    assert len(run["signal_fingerprint"]) == 64
    assert run["seed"] == 7
    assert run["intrabar_policy"] == "adverse_first"
    assert run["strategy_parameters"] == {"window": 20}
    assert run["costs"] == {
        "total_fees": "0",
        "total_funding": "0",
        "total_slippage": "0",
    }
    assert run["warnings"] == ["fixture warning"]
    assert len(run["files"]["metrics.json"]) == 64
    assert pd.read_parquet(bundle.run_dir / "equity.parquet").shape[0] == 2
    assert pd.read_parquet(bundle.run_dir / "signals.parquet")["signal_id"].tolist() == [
        "signal-1",
    ]


def test_writer_never_overwrites_existing_run(tmp_path: Path) -> None:
    writer = ArtifactWriter()
    result = complete_result()

    first = writer.write(result, metrics(), validation_result(), tmp_path)
    second = writer.write(result, metrics(), validation_result(), tmp_path)

    assert first.run_dir != second.run_dir
    assert first.run_dir.exists()
    assert second.run_dir.exists()


def complete_result(*, strategy_id: str = "fixture_strategy") -> BacktestResult:
    snapshots = (
        snapshot(datetime(2024, 1, 1, tzinfo=UTC), Decimal("100")),
        snapshot(datetime(2024, 1, 2, tzinfo=UTC), Decimal("105")),
    )
    return BacktestResult(
        run_id="run-1",
        strategy_id=strategy_id,
        data_manifests=(
            DataManifest(
                provider="fixture",
                market="spot",
                symbol="BTC/USD",
                timeframe="1d",
                requested_start=snapshots[0].timestamp,
                requested_end=datetime(2024, 1, 3, tzinfo=UTC),
                delivered_start=snapshots[0].timestamp,
                delivered_end=datetime(2024, 1, 3, tzinfo=UTC),
                retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
                real_data=False,
                raw_sha256=("0" * 64,),
                normalized_sha256="1" * 64,
            ),
        ),
        orders=(),
        fills=(),
        positions=snapshots[-1].positions,
        snapshots=snapshots,
        trades=(),
        signal_ids=("signal-1",),
        diagnostics={
            "seed": 7,
            "intrabar_policy": "adverse_first",
            "strategy_parameters": {"window": 20},
        },
        warnings=("fixture warning",),
    )


def snapshot(timestamp: datetime, equity: Decimal) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        timestamp=timestamp,
        cash=equity,
        equity=equity,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        positions=(Position(instrument=InstrumentKind.SPOT),),
    )


def metrics() -> PerformanceMetrics:
    return PerformanceMetrics(
        total_return=0.05,
        cagr=0.0,
        annualized_volatility=0.0,
        sharpe=0.0,
        sortino=0.0,
        calmar=0.0,
        omega=0.0,
        max_drawdown=0.0,
        average_drawdown=0.0,
        max_drawdown_bars=0,
        exposure=0.0,
        turnover=0.0,
        total_fees=Decimal("0"),
        total_slippage=Decimal("0"),
        total_funding=Decimal("0"),
        trade_count=0,
        win_rate=0.0,
        profit_factor=0.0,
        expectancy=0.0,
        average_holding_period_bars=0.0,
        var_95=0.0,
        cvar_95=0.0,
        periods_per_year=365.25,
        monthly_returns={},
        yearly_returns={},
        warnings=(),
    )


def validation_result() -> ValidationResult:
    return ValidationResult(
        spec=ValidationSpec(
            selection_end=datetime(2024, 1, 1, tzinfo=UTC),
            final_test_start=datetime(2024, 1, 2, tzinfo=UTC),
            final_test_end=datetime(2024, 1, 3, tzinfo=UTC),
        ),
        splits=(),
        selected_parameters=(),
        warnings=(),
    )
