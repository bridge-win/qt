from __future__ import annotations

from decimal import Decimal

import pytest
from btc_backtest.strategies.ensemble import (
    EnsembleComponent,
    WeightedEnsemble,
)
from btc_backtest.strategies.target_weight import TargetWeightStrategy

from .test_target_weight import ConstantWeightStrategy, _context


def test_weighted_ensemble_normalizes_component_weights() -> None:
    ensemble = WeightedEnsemble(
        (
            EnsembleComponent(
                strategy=ConstantWeightStrategy(Decimal("1")),
                weight=Decimal("3"),
            ),
            EnsembleComponent(
                strategy=ConstantWeightStrategy(Decimal("0")),
                weight=Decimal("1"),
            ),
        )
    )

    assert ensemble.target_weight(
        _context(cash="1000", quantity="0", close="100")
    ) == Decimal("0.75")
    assert ensemble.component_weights == (Decimal("0.75"), Decimal("0.25"))


def test_weighted_ensemble_requires_two_or_three_target_weight_strategies() -> None:
    component = EnsembleComponent(
        strategy=ConstantWeightStrategy(Decimal("1")),
        weight=Decimal("1"),
    )
    with pytest.raises(ValueError, match="two or three"):
        WeightedEnsemble((component,))
    with pytest.raises(ValueError, match="two or three"):
        WeightedEnsemble((component, component, component, component))


def test_weighted_ensemble_rejects_non_positive_weights() -> None:
    first: TargetWeightStrategy = ConstantWeightStrategy(Decimal("1"))
    second: TargetWeightStrategy = ConstantWeightStrategy(Decimal("0"))
    with pytest.raises(ValueError, match="positive"):
        WeightedEnsemble(
            (
                EnsembleComponent(strategy=first, weight=Decimal("0")),
                EnsembleComponent(strategy=second, weight=Decimal("1")),
            )
        )
