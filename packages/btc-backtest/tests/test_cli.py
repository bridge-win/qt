from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from btc_backtest.cli import app
from typer.testing import CliRunner

RUNNER = CliRunner()
PACKAGE_ROOT = Path(__file__).parents[1]


def _parquet(path: Path) -> None:
    pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [102.0, 103.0, 104.0, 105.0],
            "low": [99.0, 100.0, 101.0, 102.0],
            "close": [101.0, 102.0, 103.0, 104.0],
            "volume": [10.0, 11.0, 12.0, 13.0],
        },
        index=pd.date_range("2024-01-01", periods=4, freq="1D", tz="UTC"),
    ).to_parquet(path)


def test_run_custom_cli_exports_json(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "btc.parquet"
    _parquet(fixture)
    strategy = PACKAGE_ROOT / "examples" / "custom_strategy.py"

    result = RUNNER.invoke(
        app,
        [
            "run-custom",
            f"{strategy}:CustomStrategy",
            "--provider",
            "local",
            "--path",
            str(fixture),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--fee-bps",
            "0",
            "--slippage-bps",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["strategy_id"] == "custom_sma"
    assert payload["data_manifests"][0]["provider"] == "local"
    assert payload["snapshots"]


def test_data_sync_and_inspect_use_the_same_cache_entry(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "btc.parquet"
    cache = tmp_path / "cache"
    _parquet(fixture)
    common = [
        "--provider",
        "local",
        "--symbol",
        "BTC/USD",
        "--timeframe",
        "1d",
        "--start",
        "2024-01-01T00:00:00Z",
        "--end",
        "2024-01-05T00:00:00Z",
        "--cache-dir",
        str(cache),
    ]

    synced = RUNNER.invoke(
        app,
        ["data", "sync", *common, "--path", str(fixture)],
    )
    inspected = RUNNER.invoke(app, ["data", "inspect", *common])

    assert synced.exit_code == 0, synced.output
    assert inspected.exit_code == 0, inspected.output
    sync_payload = json.loads(synced.stdout)
    inspect_payload = json.loads(inspected.stdout)
    assert sync_payload["normalized_sha256"] == inspect_payload["normalized_sha256"]


def test_cli_exposes_required_command_groups() -> None:
    result = RUNNER.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "data" in result.stdout
    assert "strategies" in result.stdout
    assert "run-custom" in result.stdout
    assert "run" in result.stdout


def test_strategies_list_and_describe_include_complete_catalog() -> None:
    listed = RUNNER.invoke(app, ["strategies", "list", "--format", "json"])
    described = RUNNER.invoke(
        app,
        ["strategies", "describe", "funding_basis_carry"],
    )

    assert listed.exit_code == 0, listed.output
    assert described.exit_code == 0, described.output
    catalog = json.loads(listed.stdout)
    metadata = json.loads(described.stdout)
    assert len(catalog) == 23
    assert catalog[0]["id"] == "fixed_dca"
    assert catalog[19]["id"] == "funding_basis_carry"
    assert catalog[-1]["id"] == "wick_catcher"
    assert metadata["supported_instruments"] == ["spot", "perpetual"]


def test_run_builtin_cli_delegates_to_backtest_runner(tmp_path: Path) -> None:
    fixture = tmp_path / "btc.parquet"
    _parquet(fixture)

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
            "--fee-bps",
            "0",
            "--slippage-bps",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["strategy_id"] == "fixed_dca"
    assert payload["orders"]


def test_cli_returns_exit_two_with_typed_error(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "run-custom",
            f"{tmp_path / 'missing.py'}:Missing",
            "--provider",
            "local",
            "--path",
            str(tmp_path / "missing.parquet"),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
    )

    assert result.exit_code == 2
    assert "StrategyLoadError:" in result.stderr
