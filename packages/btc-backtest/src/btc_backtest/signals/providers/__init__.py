"""Built-in network signal providers."""

from btc_backtest.signals.providers.alternative import (
    FearGreedSignalProvider,
)
from btc_backtest.signals.providers.binance import (
    BINANCE_SIGNAL_RULES,
    BinanceDerivativesSignalProvider,
    BinanceSignalRule,
)
from btc_backtest.signals.providers.coinmetrics import (
    CoinMetricsSignalProvider,
    MetricRule,
)
from btc_backtest.signals.providers.generic_http import (
    EnvironmentHeader,
    GenericJSONFieldMap,
    GenericJSONSignalProvider,
    JSONProviderConfig,
)
from btc_backtest.signals.providers.local import (
    QTIntelArchiveProvider,
    SignalArchiveProvider,
)

__all__ = [
    "BINANCE_SIGNAL_RULES",
    "BinanceDerivativesSignalProvider",
    "BinanceSignalRule",
    "CoinMetricsSignalProvider",
    "EnvironmentHeader",
    "FearGreedSignalProvider",
    "GenericJSONFieldMap",
    "GenericJSONSignalProvider",
    "JSONProviderConfig",
    "MetricRule",
    "QTIntelArchiveProvider",
    "SignalArchiveProvider",
]
