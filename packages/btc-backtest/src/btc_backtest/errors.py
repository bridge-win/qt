"""Typed public exceptions raised by the backtesting package."""


class BacktestError(Exception):
    """Base class for all expected backtesting failures."""


class DataValidationError(BacktestError):
    """Market or signal data failed structural validation."""


class DataCoverageError(DataValidationError):
    """Market or signal data does not cover the requested interval."""


class ProviderError(BacktestError):
    """An external or local data provider could not satisfy a request."""


class NetworkUnavailableError(ProviderError):
    """A provider could not be reached after bounded transport retries."""


class StrategyLoadError(BacktestError):
    """A strategy could not be discovered, loaded, or validated."""


class ExecutionError(BacktestError):
    """A backtest could not complete its deterministic execution."""
