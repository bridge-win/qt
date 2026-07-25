from __future__ import annotations

from collections.abc import Sequence
from datetime import timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import InstrumentKind, PortfolioSnapshot, Position
from btc_backtest.strategies.base import StrategyContext
from btc_backtest.strategies.channels import (
    DonchianBreakout,
    DonchianBreakoutParams,
    TurtleTrend,
    TurtleTrendParams,
)
from btc_backtest.strategies.registry import default_strategy_registry
from pydantic import ValidationError

UTC = timezone.utc


def _context(
    closes: Sequence[float],
    *,
    cash: str = "10000",
) -> StrategyContext:
    index = pd.date_range("2024-01-01", periods=len(closes), freq="1D", tz=UTC)
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [10.0] * len(closes),
        },
        index=index,
    )
    timestamp = index[-1].to_pydatetime()
    return StrategyContext(
        timestamp=timestamp,
        bars=frame,
        portfolio=PortfolioSnapshot(
            timestamp=timestamp,
            cash=Decimal(cash),
            equity=Decimal(cash),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(
                Position(instrument=InstrumentKind.SPOT),
                Position(instrument=InstrumentKind.PERPETUAL),
            ),
        ),
    )


def test_donchian_uses_prior_channel_not_current_high() -> None:
    strategy = DonchianBreakout({"entry_window": 3, "exit_window": 2})

    weights = [
        strategy.target_weight(_context([10, 9, 8, 12][:end]))
        for end in range(1, 5)
    ]

    assert weights[:3] == [Decimal("0")] * 3
    assert weights[3] == Decimal("1")


def test_donchian_exits_the_shifted_short_channel() -> None:
    strategy = DonchianBreakout({"entry_window": 3, "exit_window": 2})
    prices = [10, 9, 8, 12, 11, 7]

    weights = [
        strategy.target_weight(_context(prices[:end]))
        for end in range(1, len(prices) + 1)
    ]

    assert Decimal("1") in weights
    assert weights[-1] == Decimal("0")


def test_turtle_sizes_by_atr_and_caps_available_allocation() -> None:
    strategy = TurtleTrend(
        {
            "entry_window": 3,
            "exit_window": 2,
            "risk_fraction": 0.01,
            "atr_window": 2,
            "atr_multiple": 2,
            "max_weight": 0.5,
            "rebalance_tolerance": 0,
        }
    )

    intent = strategy.on_bar(_context([10, 9, 8, 12]))[0]

    assert intent.quote_amount is not None
    assert Decimal("0") < intent.quote_amount < Decimal("10000")
    assert intent.quote_amount <= Decimal("5000")
    assert intent.reason == "turtle_entry_channel"


@pytest.mark.parametrize(
    ("model", "parameters"),
    [
        (DonchianBreakoutParams, {"entry_window": 2, "exit_window": 3}),
        (TurtleTrendParams, {"risk_fraction": 0}),
        (TurtleTrendParams, {"max_weight": 2}),
        (TurtleTrendParams, {"atr_multiple": 0}),
    ],
)
def test_channel_parameter_bounds(
    model: type[DonchianBreakoutParams] | type[TurtleTrendParams],
    parameters: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(parameters)


def test_channel_strategies_are_registered() -> None:
    registry = default_strategy_registry()

    assert isinstance(registry.create("donchian_breakout", {}), DonchianBreakout)
    assert isinstance(registry.create("turtle_trend", {}), TurtleTrend)
