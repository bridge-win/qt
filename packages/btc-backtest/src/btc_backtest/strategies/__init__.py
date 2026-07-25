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
from btc_backtest.strategies.benchmarks import BuyAndHold, BuyAndHoldParams
from btc_backtest.strategies.carry import (
    FundingBasisCarry,
    FundingBasisCarryParams,
)
from btc_backtest.strategies.channels import (
    DonchianBreakout,
    DonchianBreakoutParams,
    TurtleTrend,
    TurtleTrendParams,
)
from btc_backtest.strategies.grid import GridParams, GridRebalance
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
from btc_backtest.strategies.qt_special import (
    Capitulation,
    CapitulationParams,
    WickCatcher,
    WickCatcherParams,
)
from btc_backtest.strategies.registry import (
    BUILTIN_STRATEGY_IDS,
    EXTRA_STRATEGY_IDS,
    StrategyFactory,
    StrategyRegistry,
    default_strategy_registry,
)
from btc_backtest.strategies.target_weight import TargetWeightStrategy
from btc_backtest.strategies.volatility import (
    ATRVolatilityBreakout,
    ATRVolatilityBreakoutParams,
    KeltnerChannel,
    KeltnerChannelParams,
)
from btc_backtest.strategies.vwap import (
    VWAPMeanReversion,
    VWAPMeanReversionParams,
)

__all__ = [
    "BUILTIN_STRATEGY_IDS",
    "EXTRA_STRATEGY_IDS",
    "ADXTrend",
    "ADXTrendParams",
    "ATRVolatilityBreakout",
    "ATRVolatilityBreakoutParams",
    "BollingerBreakout",
    "BollingerBreakoutParams",
    "BollingerMeanReversion",
    "BollingerMeanReversionParams",
    "BuyAndHold",
    "BuyAndHoldParams",
    "Capitulation",
    "CapitulationParams",
    "DonchianBreakout",
    "DonchianBreakoutParams",
    "DualMomentum",
    "DualMomentumParams",
    "EMACrossover",
    "EMACrossoverParams",
    "FinalizationContext",
    "FixedDCA",
    "FixedDCAParams",
    "FundingBasisCarry",
    "FundingBasisCarryParams",
    "GridParams",
    "GridRebalance",
    "InitializationContext",
    "KeltnerChannel",
    "KeltnerChannelParams",
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
    "VWAPMeanReversion",
    "VWAPMeanReversionParams",
    "WickCatcher",
    "WickCatcherParams",
    "default_strategy_registry",
    "discover_entry_point_strategies",
    "load_strategy",
]
