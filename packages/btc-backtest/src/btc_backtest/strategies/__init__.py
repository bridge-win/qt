"""Public custom strategy SDK and loader."""

from btc_backtest.strategies.accumulation import (
    FixedDCA,
    FixedDCAParams,
    SmartDCA,
    SmartDCAParams,
)
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    Strategy,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.loader import (
    discover_entry_point_strategies,
    load_strategy,
)
from btc_backtest.strategies.moving_average import (
    EMACrossover,
    EMACrossoverParams,
    MACDTrend,
    MACDTrendParams,
    SMACrossover,
    SMACrossoverParams,
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
    "EMACrossover",
    "EMACrossoverParams",
    "FinalizationContext",
    "FixedDCA",
    "FixedDCAParams",
    "InitializationContext",
    "MACDTrend",
    "MACDTrendParams",
    "SMACrossover",
    "SMACrossoverParams",
    "SmartDCA",
    "SmartDCAParams",
    "Strategy",
    "StrategyContext",
    "StrategyFactory",
    "StrategyMetadata",
    "StrategyRegistry",
    "TargetWeightStrategy",
    "default_strategy_registry",
    "discover_entry_point_strategies",
    "load_strategy",
]
