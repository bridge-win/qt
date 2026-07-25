from __future__ import annotations

from collections.abc import Sequence
from datetime import timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import InstrumentKind, PortfolioSnapshot, Position
from btc_backtest.strategies.base import StrategyContext
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
from btc_backtest.strategies.registry import default_strategy_registry
from btc_backtest.strategies.target_weight import TargetWeightStrategy
from pydantic import ValidationError

UTC = timezone.utc


def _context(prices: Sequence[float]) -> StrategyContext:
    index = pd.date_range("2024-01-01", periods=len(prices), freq="1D", tz=UTC)
    frame = pd.DataFrame(
        {
            "open": prices,
            "high": [price + 1 for price in prices],
            "low": [price - 1 for price in prices],
            "close": prices,
            "volume": [10.0] * len(prices),
        },
        index=index,
    )
    timestamp = index[-1].to_pydatetime()
    return StrategyContext(
        timestamp=timestamp,
        bars=frame,
        portfolio=PortfolioSnapshot(
            timestamp=timestamp,
            cash=Decimal("1000"),
            equity=Decimal("1000"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(
                Position(instrument=InstrumentKind.SPOT),
                Position(instrument=InstrumentKind.PERPETUAL),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("strategy", "prices"),
    [
        (TimeSeriesMomentum({"lookback": 3}), [10, 9, 8, 11]),
        (
            RateOfChange({"lookback": 3, "entry": 0.05, "exit": 0}),
            [10, 10, 10, 11],
        ),
    ],
)
def test_absolute_momentum_enters_only_positive(
    strategy: TargetWeightStrategy,
    prices: list[float],
) -> None:
    assert strategy.target_weight(_context(prices)) == Decimal("1")


def test_dual_momentum_requires_return_above_time_scaled_cash_hurdle() -> None:
    strategy = DualMomentum({"lookback": 3, "cash_annual_rate": 0.05})

    below = strategy.target_weight(_context([10, 10, 10, 10.001]))
    above = strategy.target_weight(_context([10, 10, 10, 10.1]))

    assert below == Decimal("0")
    assert above == Decimal("1")


def test_rate_of_change_uses_entry_exit_hysteresis() -> None:
    strategy = RateOfChange({"lookback": 2, "entry": 0.05, "exit": 0})
    prices = [10, 10, 11, 10.5, 9]
    weights = [
        strategy.target_weight(_context(prices[:end]))
        for end in range(1, len(prices) + 1)
    ]

    assert Decimal("1") in weights
    assert weights[-1] == Decimal("0")


def test_adx_requires_positive_direction_and_strength() -> None:
    strategy = ADXTrend({"window": 3, "threshold": 20})

    strong = strategy.target_weight(_context([10, 11, 12, 13, 14, 15]))
    weak = strategy.target_weight(_context([15, 14, 13, 12, 11, 10]))

    assert strong == Decimal("1")
    assert weak == Decimal("0")


@pytest.mark.parametrize(
    ("model", "parameters"),
    [
        (TimeSeriesMomentumParams, {"lookback": 1}),
        (DualMomentumParams, {"cash_annual_rate": -1}),
        (RateOfChangeParams, {"entry": -0.1, "exit": 0}),
        (ADXTrendParams, {"threshold": 101}),
    ],
)
def test_momentum_parameter_bounds(
    model: type[TimeSeriesMomentumParams]
    | type[DualMomentumParams]
    | type[RateOfChangeParams]
    | type[ADXTrendParams],
    parameters: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(parameters)


def test_momentum_strategies_are_registered() -> None:
    registry = default_strategy_registry()

    assert isinstance(
        registry.create("time_series_momentum", {}),
        TimeSeriesMomentum,
    )
    assert isinstance(registry.create("dual_momentum", {}), DualMomentum)
    assert isinstance(registry.create("rate_of_change", {}), RateOfChange)
    assert isinstance(registry.create("adx_trend", {}), ADXTrend)
