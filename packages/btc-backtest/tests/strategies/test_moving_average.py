from __future__ import annotations

from collections.abc import Sequence
from datetime import timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import (
    InstrumentKind,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.strategies.base import StrategyContext
from btc_backtest.strategies.moving_average import (
    EMACrossover,
    EMACrossoverParams,
    MACDTrend,
    MACDTrendParams,
    SMACrossover,
    SMACrossoverParams,
)
from btc_backtest.strategies.registry import default_strategy_registry
from btc_backtest.strategies.target_weight import TargetWeightStrategy
from pydantic import ValidationError

UTC = timezone.utc


def _context(prices: Sequence[float]) -> StrategyContext:
    index = pd.date_range("2024-01-01", periods=len(prices), freq="1D", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": prices,
            "high": [price + 0.5 for price in prices],
            "low": [price - 0.5 for price in prices],
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


@pytest.mark.parametrize(
    "strategy",
    [
        SMACrossover({"fast_window": 2, "slow_window": 3}),
        EMACrossover({"fast_window": 2, "slow_window": 3}),
    ],
)
def test_crossover_enters_only_after_confirmed_cross(
    strategy: TargetWeightStrategy,
) -> None:
    weights = _weights(strategy, [3, 2, 1, 2, 4])

    assert weights[:4] == [Decimal("0")] * 4
    assert weights[-1] == Decimal("1")


@pytest.mark.parametrize(
    "strategy",
    [
        SMACrossover({"fast_window": 2, "slow_window": 3}),
        EMACrossover({"fast_window": 2, "slow_window": 3}),
    ],
)
def test_crossover_exits_only_after_confirmed_bearish_cross(
    strategy: TargetWeightStrategy,
) -> None:
    weights = _weights(strategy, [3, 2, 1, 2, 4, 3, 1])

    assert Decimal("1") in weights
    assert weights[-1] == Decimal("0")


def test_macd_enters_and_exits_on_completed_histogram_crosses() -> None:
    strategy = MACDTrend({"fast": 2, "slow": 4, "signal": 2})
    prices = [10, 10, 10, 10, 9, 8, 7, 8, 10, 12, 14, 12, 9, 6]

    weights = _weights(strategy, prices)

    assert Decimal("1") in weights
    assert weights[-1] == Decimal("0")


def test_adapter_reasons_identify_entry_and_exit_rule() -> None:
    strategy = SMACrossover(
        {
            "fast_window": 2,
            "slow_window": 3,
            "rebalance_tolerance": 0,
        }
    )

    entry = strategy.on_bar(_context([3, 2, 1, 2, 4]))

    assert entry[0].reason == "sma_bullish_cross"


@pytest.mark.parametrize(
    ("model", "parameters"),
    [
        (
            SMACrossoverParams,
            {"fast_window": 5, "slow_window": 3},
        ),
        (
            EMACrossoverParams,
            {"fast_window": 3, "slow_window": 3},
        ),
        (
            MACDTrendParams,
            {"fast": 4, "slow": 3, "signal": 2},
        ),
    ],
)
def test_moving_average_parameters_require_fast_below_slow(
    model: type[SMACrossoverParams]
    | type[EMACrossoverParams]
    | type[MACDTrendParams],
    parameters: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="below"):
        model.model_validate(parameters)


def test_moving_average_warmup_tracks_instance_parameters() -> None:
    sma_strategy = SMACrossover({"fast_window": 2, "slow_window": 7})
    macd_strategy = MACDTrend({"fast": 2, "slow": 5, "signal": 3})

    assert sma_strategy.metadata.warmup_bars == 8
    assert macd_strategy.metadata.warmup_bars == 8


def test_moving_average_strategies_are_registered() -> None:
    registry = default_strategy_registry()

    assert isinstance(registry.create("sma_crossover", {}), SMACrossover)
    assert isinstance(registry.create("ema_crossover", {}), EMACrossover)
    assert isinstance(registry.create("macd_trend", {}), MACDTrend)
