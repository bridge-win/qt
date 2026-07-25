"""Built-in market data provider contracts and adapters."""

from btc_backtest.data.providers.base import (
    MarketDataProvider,
    ProviderMetadata,
    ProviderRegistry,
)
from btc_backtest.data.providers.bitstamp import BitstampProvider
from btc_backtest.data.providers.composite import CompositeProvider
from btc_backtest.data.providers.local import LocalParquetProvider
from btc_backtest.data.providers.synthetic import SyntheticProvider

__all__ = [
    "BitstampProvider",
    "CompositeProvider",
    "LocalParquetProvider",
    "MarketDataProvider",
    "ProviderMetadata",
    "ProviderRegistry",
    "SyntheticProvider",
]
