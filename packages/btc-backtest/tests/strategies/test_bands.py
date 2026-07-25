from __future__ import annotations

from collections.abc import Sequence
from datetime import timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import InstrumentKind, PortfolioSnapshot, Position
from btc_backtest.strategies.bands import (
    BollingerBreakout,
    BollingerBreakoutParams,
    BollingerMeanReversion,
    BollingerMeanReversionParams,
)
from btc_backtest.strategies.base import StrategyContext
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
            "low": [max(0.01, price - 1) for price in prices],
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


def _weights(
    strategy: TargetWeightStrategy,
    prices: Sequence[float],
) -> list[Decimal]:
    return [
        strategy.target_weight(_context(prices[:end]))
        for end in range(1, len(prices) + 1)
    ]


def test_bollinger_mean_reversion_buys_lower_band_and_exits_middle() -> None:
    strategy = BollingerMeanReversion({"window": 3, "stddev": 1.0})

    weights = _weights(strategy, [10, 10, 10, 7, 9, 10])

    assert weights[3] == Decimal("1")
    assert weights[-1] == Decimal("0")


def test_bollinger_breakout_enters_upper_band_and_uses_trailing_exit() -> None:
    strategy = BollingerBreakout(
        {
            "window": 3,
            "stddev": 1.0,
            "atr_window": 2,
            "atr_stop": 1.5,
        }
    )

    weights = _weights(strategy, [10, 10, 13, 15, 14, 8])

    assert weights[2] == Decimal("1")
    assert weights[-1] == Decimal("0")
    assert strategy.entry_reason == "upper_band_breakout"
    assert strategy.exit_reason == "atr_trailing_exit"


def test_breakout_trailing_stop_never_moves_down() -> None:
    strategy = BollingerBreakout(
        {
            "window": 3,
            "stddev": 1.0,
            "atr_window": 2,
            "atr_stop": 1.0,
        }
    )
    observed: list[Decimal] = []
    for end in range(1, 6):
        strategy.target_weight(_context([10, 10, 13, 15, 14][:end]))
        if strategy.trailing_stop is not None:
            observed.append(strategy.trailing_stop)

    assert observed == sorted(observed)


@pytest.mark.parametrize(
    ("model", "parameters"),
    [
        (BollingerMeanReversionParams, {"window": 1}),
        (BollingerMeanReversionParams, {"stddev": 0}),
        (BollingerBreakoutParams, {"atr_stop": -1}),
        (BollingerBreakoutParams, {"atr_window": 1}),
    ],
)
def test_bollinger_parameter_bounds(
    model: type[BollingerMeanReversionParams]
    | type[BollingerBreakoutParams],
    parameters: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(parameters)


def test_bollinger_strategies_are_registered() -> None:
    registry = default_strategy_registry()

    assert isinstance(
        registry.create("bollinger_mean_reversion", {}),
        BollingerMeanReversion,
    )
    assert isinstance(
        registry.create("bollinger_breakout", {}),
        BollingerBreakout,
    )
