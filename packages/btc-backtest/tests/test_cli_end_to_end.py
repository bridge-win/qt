from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from btc_backtest.cli import app
from typer.testing import CliRunner

RUNNER = CliRunner()


def test_run_writes_metrics_and_artifact_bundle(tmp_path: Path) -> None:
    fixture = tmp_path / "btc.parquet"
    _parquet(fixture, periods=8)
    output = tmp_path / "runs"

    result = RUNNER.invoke(
        app,
        [
            "run",
            "fixed_dca",
            "--provider",
            "local",
            "--path",
            str(fixture),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output",
            str(output),
            "--fee-bps",
            "0",
            "--slippage-bps",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    run_dir = Path(payload["artifact_dir"])
    assert payload["strategy_id"] == "fixed_dca"
    assert payload["metrics"]["trade_count"] >= 0
    assert run_dir.parent == output
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "report.html").is_file()


def test_run_resolves_years_interval_and_labels_synthetic_data(
    tmp_path: Path,
) -> None:
    result = RUNNER.invoke(
        app,
        [
            "run",
            "fixed_dca",
            "--provider",
            "synthetic",
            "--synthetic",
            "--years",
            "1",
            "--end",
            "2024-01-05T00:00:00Z",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output",
            str(tmp_path / "runs"),
            "--fee-bps",
            "0",
            "--slippage-bps",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    manifest = payload["data_manifests"][0]
    assert payload["synthetic"] is True
    assert manifest["real_data"] is False
    assert manifest["requested_start"] == "2023-01-05T00:00:00Z"
    assert manifest["requested_end"] == "2024-01-05T00:00:00Z"


def test_validate_writes_walk_forward_artifact_bundle(tmp_path: Path) -> None:
    fixture = tmp_path / "btc.parquet"
    _parquet(fixture, periods=10)
    output = tmp_path / "validation-runs"

    result = RUNNER.invoke(
        app,
        [
            "validate",
            "fixed_dca",
            "--provider",
            "local",
            "--path",
            str(fixture),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output",
            str(output),
            "--train-bars",
            "2",
            "--test-bars",
            "2",
            "--final-test-bars",
            "2",
            "--candidate-json",
            "{}",
            "--fee-bps",
            "0",
            "--slippage-bps",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    run_dir = Path(payload["artifact_dir"])
    assert payload["strategy_id"] == "fixed_dca"
    assert len(payload["walk_forward"]["windows"]) == 2
    assert payload["validation"]["selected_parameters"] == [{}, {}]
    assert run_dir.parent == output
    assert (run_dir / "validation.json").is_file()
    assert (run_dir / "report.html").is_file()


def _parquet(path: Path, *, periods: int) -> None:
    close = [100.0 + float(index) for index in range(periods)]
    pd.DataFrame(
        {
            "open": close,
            "high": [value + 1.0 for value in close],
            "low": [value - 1.0 for value in close],
            "close": close,
            "volume": [10.0 + float(index) for index in range(periods)],
        },
        index=pd.date_range("2024-01-01", periods=periods, freq="1D", tz="UTC"),
    ).to_parquet(path)
