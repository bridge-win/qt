"""Public custom strategy SDK and loader."""

from btc_backtest.strategies.accumulation import (
    FixedDCA,
    FixedDCAParams,
    SmartDCA,
    SmartDCAParams,
)
from btc_backtest.strategies.bands import (
    BollingerBreakout,
    BollingerBreakoutParams,
    BollingerMeanReversion,
    BollingerMeanReversionParams,
)
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    Strategy,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.channels import (
    DonchianBreakout,
    DonchianBreakoutParams,
    TurtleTrend,
    TurtleTrendParams,
)
from btc_backtest.strategies.loader import (
    discover_entry_point_strategies,
    load_strategy,
)
from btc_backtest.strategies.momentum import (
    ADXTrend,
    ADXTrendParams,
    DualMomentum,
    DualMomentumParams,
    RateOfChange,
    RateOfChangeParams,
    TimeSeriesMomentum,
    TimeSeriesMomentumParams,
)
from btc_backtest.strategies.moving_average import (
    EMACrossover,
    EMACrossoverParams,
    MACDTrend,
    MACDTrendParams,
    SMACrossover,
    SMACrossoverParams,
)
from btc_backtest.strategies.oscillators import (
    RSIMeanReversion,
    RSIMeanReversionParams,
    StochasticReversal,
    StochasticReversalParams,
)
from btc_backtest.strategies.registry import (
    BUILTIN_STRATEGY_IDS,
    EXTRA_STRATEGY_IDS,
    StrategyFactory,
    StrategyRegistry,
    default_strategy_registry,
)
from btc_backtest.strategies.target_weight import TargetWeightStrategy

__all__ = [
    "BUILTIN_STRATEGY_IDS",
    "EXTRA_STRATEGY_IDS",
    "ADXTrend",
    "ADXTrendParams",
    "BollingerBreakout",
    "BollingerBreakoutParams",
    "BollingerMeanReversion",
    "BollingerMeanReversionParams",
    "DonchianBreakout",
    "DonchianBreakoutParams",
    "DualMomentum",
    "DualMomentumParams",
    "EMACrossover",
    "EMACrossoverParams",
    "FinalizationContext",
    "FixedDCA",
    "FixedDCAParams",
    "InitializationContext",
    "MACDTrend",
    "MACDTrendParams",
    "RSIMeanReversion",
    "RSIMeanReversionParams",
    "RateOfChange",
    "RateOfChangeParams",
    "SMACrossover",
    "SMACrossoverParams",
    "SmartDCA",
    "SmartDCAParams",
    "StochasticReversal",
    "StochasticReversalParams",
    "Strategy",
    "StrategyContext",
    "StrategyFactory",
    "StrategyMetadata",
    "StrategyRegistry",
    "TargetWeightStrategy",
    "TimeSeriesMomentum",
    "TimeSeriesMomentumParams",
    "TurtleTrend",
    "TurtleTrendParams",
    "default_strategy_registry",
    "discover_entry_point_strategies",
    "load_strategy",
]
