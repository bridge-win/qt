from __future__ import annotations

from collections.abc import Sequence
from datetime import timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import InstrumentKind, PortfolioSnapshot, Position
from btc_backtest.strategies.base import StrategyContext
from btc_backtest.strategies.registry import default_strategy_registry
from btc_backtest.strategies.volatility import (
    ATRVolatilityBreakout,
    ATRVolatilityBreakoutParams,
    KeltnerChannel,
    KeltnerChannelParams,
)
from pydantic import ValidationError

UTC = timezone.utc


def _context(prices: Sequence[float]) -> StrategyContext:
    index = pd.date_range("2024-01-01", periods=len(prices), freq="1D", tz=UTC)
    frame = pd.DataFrame(
        {
            "open": prices,
            "high": [value + 1 for value in prices],
            "low": [value - 1 for value in prices],
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


def test_atr_breakout_uses_prior_close_plus_prior_atr() -> None:
    strategy = ATRVolatilityBreakout(
        {"atr_window": 3, "multiplier": 1.0}
    )

    weight = strategy.target_weight(_context([10, 10, 10, 14]))

    assert weight == Decimal("1")


def test_keltner_enters_upper_channel_and_exits_center() -> None:
    strategy = KeltnerChannel(
        {"ema_window": 3, "atr_window": 2, "multiplier": 0.5}
    )
    prices = [10, 10, 11, 12, 14, 10]
    weights = [
        strategy.target_weight(_context(prices[:end]))
        for end in range(1, len(prices) + 1)
    ]

    assert Decimal("1") in weights
    assert weights[-1] == Decimal("0")
    assert strategy.entry_reason == "keltner_breakout"
    assert strategy.exit_reason == "keltner_center_exit"


@pytest.mark.parametrize(
    ("model", "parameters"),
    [
        (ATRVolatilityBreakoutParams, {"atr_window": 1}),
        (ATRVolatilityBreakoutParams, {"multiplier": 0}),
        (KeltnerChannelParams, {"ema_window": 1}),
        (KeltnerChannelParams, {"multiplier": -1}),
    ],
)
def test_volatility_parameter_bounds(
    model: type[ATRVolatilityBreakoutParams] | type[KeltnerChannelParams],
    parameters: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(parameters)


def test_volatility_strategies_are_registered() -> None:
    registry = default_strategy_registry()

    assert isinstance(
        registry.create("atr_volatility_breakout", {}),
        ATRVolatilityBreakout,
    )
    assert isinstance(registry.create("keltner_channel", {}), KeltnerChannel)
