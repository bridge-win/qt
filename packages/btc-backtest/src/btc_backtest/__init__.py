"""Public package surface for the independent BTC backtester."""

__version__ = "0.1.0"

from btc_backtest.api import BacktestRunner
from btc_backtest.errors import (
    BacktestError,
    DataCoverageError,
    DataValidationError,
    ExecutionError,
    NetworkUnavailableError,
    ProviderError,
    StrategyLoadError,
)

__all__ = [
    "BacktestError",
    "BacktestRunner",
    "DataCoverageError",
    "DataValidationError",
    "ExecutionError",
    "NetworkUnavailableError",
    "ProviderError",
    "StrategyLoadError",
    "__version__",
]
