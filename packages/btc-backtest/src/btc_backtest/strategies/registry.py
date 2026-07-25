"""Typed registry and exact catalog for built-in BTC strategies."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from btc_backtest.errors import StrategyLoadError
from btc_backtest.strategies.base import Strategy, StrategyMetadata

BUILTIN_STRATEGY_IDS = (
    "fixed_dca",
    "smart_dca",
    "sma_crossover",
    "ema_crossover",
    "macd_trend",
    "rsi_mean_reversion",
    "stochastic_reversal",
    "bollinger_mean_reversion",
    "bollinger_breakout",
    "donchian_breakout",
    "turtle_trend",
    "time_series_momentum",
    "dual_momentum",
    "rate_of_change",
    "adx_trend",
    "atr_volatility_breakout",
    "keltner_channel",
    "vwap_mean_reversion",
    "grid_rebalance",
    "funding_basis_carry",
)
EXTRA_STRATEGY_IDS = (
    "buy_and_hold",
    "capitulation",
    "wick_catcher",
)
StrategyFactory = Callable[[Mapping[str, object]], Strategy]


class StrategyRegistry:
    """Register factories without allowing aliases to mutate the catalog."""

    def __init__(self) -> None:
        self._factories: dict[str, StrategyFactory] = {}
        self._metadata: dict[str, StrategyMetadata] = {}
        self._aliases: dict[str, str] = {}

    def register(
        self,
        strategy_id: str,
        factory: StrategyFactory,
        metadata: StrategyMetadata,
    ) -> None:
        if strategy_id != metadata.id:
            raise StrategyLoadError(
                f"registry id {strategy_id} does not match metadata id "
                f"{metadata.id}"
            )
        if strategy_id in self._factories or strategy_id in self._aliases:
            raise StrategyLoadError(
                f"duplicate strategy registration: {strategy_id}"
            )
        self._factories[strategy_id] = factory
        self._metadata[strategy_id] = metadata

    def register_alias(self, alias: str, target: str) -> None:
        if (
            alias in self._aliases
            or alias in self._factories
            or alias in BUILTIN_STRATEGY_IDS
            or alias in EXTRA_STRATEGY_IDS
        ):
            raise StrategyLoadError(f"duplicate strategy alias: {alias}")
        known_targets = (
            set(self._factories)
            | set(BUILTIN_STRATEGY_IDS)
            | set(EXTRA_STRATEGY_IDS)
        )
        if target not in known_targets:
            raise StrategyLoadError(f"unknown alias target: {target}")
        self._aliases[alias] = target

    def create(
        self,
        strategy_id: str,
        parameters: Mapping[str, object],
    ) -> Strategy:
        resolved = self._aliases.get(strategy_id, strategy_id)
        factory = self._factories.get(resolved)
        if factory is None:
            if resolved in BUILTIN_STRATEGY_IDS or resolved in EXTRA_STRATEGY_IDS:
                raise StrategyLoadError(
                    f"strategy implementation not registered: {resolved}"
                )
            raise StrategyLoadError(f"unknown strategy: {strategy_id}")
        isolated = MappingProxyType(dict(parameters))
        try:
            strategy = factory(isolated)
        except Exception as error:
            raise StrategyLoadError(
                f"failed to construct strategy: {resolved}"
            ) from error
        if not isinstance(strategy, Strategy):
            raise StrategyLoadError(
                f"registered factory {resolved} did not return a Strategy"
            )
        if strategy.metadata.id != resolved:
            raise StrategyLoadError(
                f"registered factory {resolved} returned metadata id "
                f"{strategy.metadata.id}"
            )
        return strategy

    def list(self) -> tuple[str, ...]:
        """Return registered canonical IDs in stable catalog order."""

        catalog_order = BUILTIN_STRATEGY_IDS + EXTRA_STRATEGY_IDS
        ordered = [
            strategy_id
            for strategy_id in catalog_order
            if strategy_id in self._factories
        ]
        catalog = set(catalog_order)
        ordered.extend(
            sorted(
                strategy_id
                for strategy_id in self._factories
                if strategy_id not in catalog
            )
        )
        return tuple(ordered)

    def describe(self, strategy_id: str) -> StrategyMetadata:
        resolved = self._aliases.get(strategy_id, strategy_id)
        metadata = self._metadata.get(resolved)
        if metadata is not None:
            return metadata
        if resolved in BUILTIN_STRATEGY_IDS or resolved in EXTRA_STRATEGY_IDS:
            raise StrategyLoadError(
                f"strategy implementation not registered: {resolved}"
            )
        raise StrategyLoadError(f"unknown strategy: {strategy_id}")

    @property
    def factories(self) -> Mapping[str, StrategyFactory]:
        return MappingProxyType(dict(self._factories))

    @property
    def strategies(self) -> Mapping[str, StrategyFactory]:
        """Compatibility name consumed by ``BacktestRunner``."""

        return self.factories

    @property
    def aliases(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self._aliases))


def default_strategy_registry() -> StrategyRegistry:
    """Build a fresh registry containing every implemented built-in."""

    from btc_backtest.strategies.accumulation import FixedDCA, SmartDCA
    from btc_backtest.strategies.bands import (
        BollingerBreakout,
        BollingerMeanReversion,
    )
    from btc_backtest.strategies.channels import DonchianBreakout, TurtleTrend
    from btc_backtest.strategies.grid import GridRebalance
    from btc_backtest.strategies.momentum import (
        ADXTrend,
        DualMomentum,
        RateOfChange,
        TimeSeriesMomentum,
    )
    from btc_backtest.strategies.moving_average import (
        EMACrossover,
        MACDTrend,
        SMACrossover,
    )
    from btc_backtest.strategies.oscillators import (
        RSIMeanReversion,
        StochasticReversal,
    )
    from btc_backtest.strategies.volatility import (
        ATRVolatilityBreakout,
        KeltnerChannel,
    )
    from btc_backtest.strategies.vwap import VWAPMeanReversion

    registry = StrategyRegistry()
    registry.register(
        "fixed_dca",
        lambda parameters: FixedDCA(parameters),
        FixedDCA.metadata,
    )
    registry.register(
        "smart_dca",
        lambda parameters: SmartDCA(parameters),
        SmartDCA.metadata,
    )
    registry.register(
        "sma_crossover",
        lambda parameters: SMACrossover(parameters),
        SMACrossover.metadata,
    )
    registry.register(
        "ema_crossover",
        lambda parameters: EMACrossover(parameters),
        EMACrossover.metadata,
    )
    registry.register(
        "macd_trend",
        lambda parameters: MACDTrend(parameters),
        MACDTrend.metadata,
    )
    registry.register(
        "rsi_mean_reversion",
        lambda parameters: RSIMeanReversion(parameters),
        RSIMeanReversion.metadata,
    )
    registry.register(
        "stochastic_reversal",
        lambda parameters: StochasticReversal(parameters),
        StochasticReversal.metadata,
    )
    registry.register(
        "bollinger_mean_reversion",
        lambda parameters: BollingerMeanReversion(parameters),
        BollingerMeanReversion.metadata,
    )
    registry.register(
        "bollinger_breakout",
        lambda parameters: BollingerBreakout(parameters),
        BollingerBreakout.metadata,
    )
    registry.register(
        "donchian_breakout",
        lambda parameters: DonchianBreakout(parameters),
        DonchianBreakout.metadata,
    )
    registry.register(
        "turtle_trend",
        lambda parameters: TurtleTrend(parameters),
        TurtleTrend.metadata,
    )
    registry.register(
        "time_series_momentum",
        lambda parameters: TimeSeriesMomentum(parameters),
        TimeSeriesMomentum.metadata,
    )
    registry.register(
        "dual_momentum",
        lambda parameters: DualMomentum(parameters),
        DualMomentum.metadata,
    )
    registry.register(
        "rate_of_change",
        lambda parameters: RateOfChange(parameters),
        RateOfChange.metadata,
    )
    registry.register(
        "adx_trend",
        lambda parameters: ADXTrend(parameters),
        ADXTrend.metadata,
    )
    registry.register(
        "atr_volatility_breakout",
        lambda parameters: ATRVolatilityBreakout(parameters),
        ATRVolatilityBreakout.metadata,
    )
    registry.register(
        "keltner_channel",
        lambda parameters: KeltnerChannel(parameters),
        KeltnerChannel.metadata,
    )
    registry.register(
        "vwap_mean_reversion",
        lambda parameters: VWAPMeanReversion(parameters),
        VWAPMeanReversion.metadata,
    )
    registry.register(
        "grid_rebalance",
        lambda parameters: GridRebalance(parameters),
        GridRebalance.metadata,
    )
    return registry
