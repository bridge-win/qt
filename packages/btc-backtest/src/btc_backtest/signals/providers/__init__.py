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

__all__ = [
    "BINANCE_SIGNAL_RULES",
    "BinanceDerivativesSignalProvider",
    "BinanceSignalRule",
    "CoinMetricsSignalProvider",
    "FearGreedSignalProvider",
    "MetricRule",
]
