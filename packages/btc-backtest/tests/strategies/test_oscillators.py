from __future__ import annotations

from collections.abc import Sequence
from datetime import timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import InstrumentKind, PortfolioSnapshot, Position
from btc_backtest.strategies.base import StrategyContext
from btc_backtest.strategies.oscillators import (
    RSIMeanReversion,
    RSIMeanReversionParams,
    StochasticReversal,
    StochasticReversalParams,
)
from btc_backtest.strategies.registry import default_strategy_registry
from btc_backtest.strategies.target_weight import TargetWeightStrategy
from pydantic import ValidationError

UTC = timezone.utc


def _context(
    closes: Sequence[float],
    *,
    high: Sequence[float] | None = None,
    low: Sequence[float] | None = None,
) -> StrategyContext:
    index = pd.date_range("2024-01-01", periods=len(closes), freq="1D", tz=UTC)
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": high or [value + 1 for value in closes],
            "low": low or [value - 1 for value in closes],
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
    closes: Sequence[float],
    *,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
) -> list[Decimal]:
    return [
        strategy.target_weight(
            _context(
                closes[:end],
                high=highs[:end] if highs is not None else None,
                low=lows[:end] if lows is not None else None,
            )
        )
        for end in range(1, len(closes) + 1)
    ]


def test_rsi_enters_oversold_and_exits_on_normalization() -> None:
    strategy = RSIMeanReversion({"window": 3, "entry": 25, "exit": 55})

    weights = _weights(strategy, [10, 9, 8, 7, 8, 9, 10])

    assert Decimal("1") in weights
    assert weights[-1] == Decimal("0")


def test_stochastic_requires_completed_crosses_in_entry_and_exit_zones() -> None:
    strategy = StochasticReversal(
        {"k_window": 3, "d_window": 2, "entry": 20, "exit": 80}
    )
    target_k = [15, 10, 5, 4, 8, 90, 95, 85]
    closes = [1 + 0.99 * value for value in target_k]
    highs = [100.0] * len(closes)
    lows = [1.0] * len(closes)

    weights = _weights(strategy, closes, highs=highs, lows=lows)

    assert weights[4] == Decimal("1")
    assert weights[-1] == Decimal("0")


def test_oscillators_do_not_backfill_warmup_signals() -> None:
    rsi_strategy = RSIMeanReversion({"window": 3})
    stochastic_strategy = StochasticReversal(
        {"k_window": 3, "d_window": 2}
    )

    assert _weights(rsi_strategy, [10, 9, 8]) == [Decimal("0")] * 3
    assert _weights(stochastic_strategy, [10, 9, 8]) == [
        Decimal("0")
    ] * 3


@pytest.mark.parametrize(
    ("model", "parameters"),
    [
        (RSIMeanReversionParams, {"entry": 60, "exit": 50}),
        (StochasticReversalParams, {"entry": 90, "exit": 80}),
        (RSIMeanReversionParams, {"window": 1}),
    ],
)
def test_oscillator_parameter_bounds(
    model: type[RSIMeanReversionParams] | type[StochasticReversalParams],
    parameters: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(parameters)


def test_oscillator_strategies_are_registered() -> None:
    registry = default_strategy_registry()

    assert isinstance(registry.create("rsi_mean_reversion", {}), RSIMeanReversion)
    assert isinstance(
        registry.create("stochastic_reversal", {}),
        StochasticReversal,
    )
