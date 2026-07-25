"""Public package surface for the independent BTC backtester."""

from btc_backtest.errors import (
    BacktestError,
    DataCoverageError,
    DataValidationError,
    ExecutionError,
    NetworkUnavailableError,
    ProviderError,
    StrategyLoadError,
)

__version__ = "0.1.0"

__all__ = [
    "BacktestError",
    "DataCoverageError",
    "DataValidationError",
    "ExecutionError",
    "NetworkUnavailableError",
    "ProviderError",
    "StrategyLoadError",
    "__version__",
]
