# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

from ..schemas import ExternalDataProvider
from .alternative_me import AlternativeMeProvider
from .binance_futures import BinanceFuturesProvider
from .binance_spot import BinanceSpotProvider
from .blockchain_com import BlockchainComProvider
from .coinmetrics import CoinMetricsProvider
from .coinglass import CoinGlassProvider
from .gdelt import GdeltProvider
from .glassnode import GlassnodeProvider
from .kaiko import KaikoProvider
from .santiment import SantimentProvider

def all_providers() -> tuple[ExternalDataProvider, ...]:
    return (
        BinanceSpotProvider(),
        BinanceFuturesProvider(),
        AlternativeMeProvider(),
        GdeltProvider(),
        CoinMetricsProvider(),
        BlockchainComProvider(),
        GlassnodeProvider(),
        SantimentProvider(),
        CoinGlassProvider(),
        KaikoProvider(),
    )

