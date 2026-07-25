from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import (
    InstrumentKind,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.strategies.base import StrategyContext
from btc_backtest.strategies.benchmarks import BuyAndHold, BuyAndHoldParams
from btc_backtest.strategies.registry import default_strategy_registry
from pydantic import ValidationError

UTC = timezone.utc


def _context() -> StrategyContext:
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    bars = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [10.0],
        },
        index=pd.DatetimeIndex([timestamp]),
    )
    return StrategyContext(
        timestamp=timestamp,
        bars=bars,
        portfolio=PortfolioSnapshot(
            timestamp=timestamp,
            cash=Decimal("10000"),
            equity=Decimal("10000"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(
                Position(instrument=InstrumentKind.SPOT),
                Position(instrument=InstrumentKind.PERPETUAL),
            ),
        ),
    )


def test_buy_and_hold_buys_once_and_never_rebalances() -> None:
    strategy = BuyAndHold({})

    first = strategy.on_bar(_context())
    second = strategy.on_bar(_context())

    assert len(first) == 1
    assert first[0].reason == "initial_buy"
    assert second == ()


def test_buy_and_hold_is_registered_as_an_extra() -> None:
    assert isinstance(
        default_strategy_registry().create("buy_and_hold", {}),
        BuyAndHold,
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"allocation": 0},
        {"allocation": 1.1},
        {"rebalance_tolerance": -0.1},
        {"unknown": True},
    ],
)
def test_buy_and_hold_parameter_bounds(parameters: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BuyAndHoldParams.model_validate(parameters)
