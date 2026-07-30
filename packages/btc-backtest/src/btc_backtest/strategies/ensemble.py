"""Explainable weighted ensembles for compatible target-weight strategies."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.target_weight import TargetWeightStrategy


@dataclass(frozen=True)
class EnsembleComponent:
    strategy: TargetWeightStrategy
    weight: Decimal

    def __post_init__(self) -> None:
        if not self.weight.is_finite() or self.weight <= 0:
            raise ValueError("ensemble component weight must be positive and finite")


class WeightedEnsemble(TargetWeightStrategy):
    """Combine two or three desired BTC exposures with normalized weights."""

    def __init__(self, components: tuple[EnsembleComponent, ...]) -> None:
        if not 2 <= len(components) <= 3:
            raise ValueError("weighted ensemble requires two or three components")
        super().__init__()
        total = sum((component.weight for component in components), Decimal("0"))
        if total <= 0:
            raise ValueError("ensemble component weights must total a positive value")
        self.components = components
        self.component_weights = tuple(
            component.weight / total for component in components
        )
        timeframes = set(components[0].strategy.metadata.supported_timeframes)
        for component in components[1:]:
            timeframes.intersection_update(
                component.strategy.metadata.supported_timeframes
            )
        if not timeframes:
            raise ValueError("ensemble components have no shared timeframe")
        self.metadata = StrategyMetadata(
            id="weighted_ensemble",
            version="1.0.0",
            description="Weighted desired BTC exposure from two or three strategies.",
            warmup_bars=max(
                component.strategy.metadata.warmup_bars
                for component in components
            ),
            supported_timeframes=tuple(sorted(timeframes)),
            requires_full_history=any(
                component.strategy.metadata.requires_full_history
                for component in components
            ),
        )

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        for component in self.components:
            component.strategy.initialize(context)

    def target_weight(self, context: StrategyContext) -> Decimal:
        return sum(
            (
                weight * component.strategy.target_weight(context)
                for component, weight in zip(
                    self.components,
                    self.component_weights,
                    strict=True,
                )
            ),
            Decimal("0"),
        )

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        direction = "increase" if target_value > current_value else "decrease"
        return f"weighted_ensemble_{direction}"

    def finalize(self, context: FinalizationContext) -> None:
        for component in self.components:
            component.strategy.finalize(context)
