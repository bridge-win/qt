from importlib.metadata import requires, version

import btc_backtest
from btc_backtest.cli import app
from btc_backtest.errors import (
    BacktestError,
    DataCoverageError,
    DataValidationError,
    ExecutionError,
    ProviderError,
    StrategyLoadError,
)
from typer.testing import CliRunner


def test_independent_distribution_exports_version() -> None:
    assert btc_backtest.__version__ == "0.1.0"
    assert version("btc-backtest") == "0.1.0"


def test_public_errors_share_the_package_base_error() -> None:
    public_errors = (
        DataCoverageError,
        DataValidationError,
        ExecutionError,
        ProviderError,
        StrategyLoadError,
    )

    assert all(issubclass(error, BacktestError) for error in public_errors)


def test_distribution_does_not_depend_on_qt() -> None:
    dependencies = requires("btc-backtest") or []

    assert not any(dependency.casefold().startswith("qt") for dependency in dependencies)


def test_console_entry_point_has_working_help() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Independent BTC backtesting" in result.stdout
