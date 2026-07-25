from __future__ import annotations

from collections.abc import Sequence
from datetime import timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import InstrumentKind, PortfolioSnapshot, Position
from btc_backtest.strategies.base import StrategyContext
from btc_backtest.strategies.registry import default_strategy_registry
from btc_backtest.strategies.vwap import (
    VWAPMeanReversion,
    VWAPMeanReversionParams,
)
from pydantic import ValidationError

UTC = timezone.utc


def _context(
    prices: Sequence[float],
    volumes: Sequence[float],
) -> StrategyContext:
    index = pd.date_range("2024-01-01", periods=len(prices), freq="1D", tz=UTC)
    frame = pd.DataFrame(
        {
            "open": prices,
            "high": [value + 1 for value in prices],
            "low": [value - 1 for value in prices],
            "close": prices,
            "volume": volumes,
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


def test_vwap_enters_negative_zscore_and_reverts_to_center() -> None:
    strategy = VWAPMeanReversion(
        {"window": 3, "entry_z": -1.0, "exit_z": 0.0}
    )
    prices = [10, 10, 10, 7, 9, 10]
    weights = [
        strategy.target_weight(
            _context(prices[:end], [10.0] * end)
        )
        for end in range(1, len(prices) + 1)
    ]

    assert Decimal("1") in weights
    assert weights[-1] == Decimal("0")


def test_vwap_zero_volume_window_never_trades() -> None:
    strategy = VWAPMeanReversion({"window": 3})

    assert (
        strategy.target_weight(_context([10, 9, 8], [0, 0, 0]))
        == Decimal("0")
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"window": 1},
        {"entry_z": 1, "exit_z": 0},
        {"entry_z": -1, "exit_z": -2},
    ],
)
def test_vwap_parameter_bounds(parameters: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VWAPMeanReversionParams.model_validate(parameters)


def test_vwap_strategy_is_registered() -> None:
    assert isinstance(
        default_strategy_registry().create("vwap_mean_reversion", {}),
        VWAPMeanReversion,
    )
