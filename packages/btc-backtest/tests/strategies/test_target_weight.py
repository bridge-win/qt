from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import (
    InstrumentKind,
    OrderSide,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.strategies.base import StrategyContext, StrategyMetadata
from btc_backtest.strategies.target_weight import TargetWeightStrategy
from pydantic import ValidationError

UTC = timezone.utc


class ConstantWeightStrategy(TargetWeightStrategy):
    metadata = StrategyMetadata(
        id="constant_weight",
        version="1.0.0",
        description="Constant target weight fixture.",
        warmup_bars=0,
        supported_timeframes=("1d",),
    )

    def __init__(
        self,
        weight: Decimal,
        *,
        tolerance: Decimal = Decimal("0"),
    ) -> None:
        super().__init__({"rebalance_tolerance": tolerance})
        self.weight = weight

    def target_weight(self, context: StrategyContext) -> Decimal:
        return self.weight


class CappedWeightStrategy(ConstantWeightStrategy):
    metadata = ConstantWeightStrategy.metadata.model_copy(
        update={"id": "capped_weight", "max_weight": Decimal("0.75")},
    )


def _context(
    *,
    cash: str,
    quantity: str,
    close: str,
    average_price: str = "100",
) -> StrategyContext:
    price = float(close)
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    bars = pd.DataFrame(
        {
            "open": [price],
            "high": [price],
            "low": [price],
            "close": [price],
            "volume": [10.0],
        },
        index=pd.DatetimeIndex([timestamp]),
    )
    spot = Position(
        instrument=InstrumentKind.SPOT,
        quantity=Decimal(quantity),
        average_price=(
            Decimal(average_price)
            if Decimal(quantity) > 0
            else Decimal("0")
        ),
    )
    equity = Decimal(cash) + Decimal(quantity) * Decimal(close)
    return StrategyContext(
        timestamp=timestamp,
        bars=bars,
        portfolio=PortfolioSnapshot(
            timestamp=timestamp,
            cash=Decimal(cash),
            equity=equity,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(
                spot,
                Position(instrument=InstrumentKind.PERPETUAL),
            ),
        ),
    )


def test_target_weight_adapter_buys_only_the_cash_needed() -> None:
    strategy = ConstantWeightStrategy(Decimal("0.50"))

    intents = strategy.on_bar(
        _context(cash="1000", quantity="0", close="100")
    )

    assert len(intents) == 1
    assert intents[0].side is OrderSide.BUY
    assert intents[0].quote_amount == Decimal("500")


def test_target_weight_adapter_sells_only_excess_inventory() -> None:
    strategy = ConstantWeightStrategy(Decimal("0.25"))

    intents = strategy.on_bar(
        _context(cash="500", quantity="5", close="100")
    )

    assert len(intents) == 1
    assert intents[0].side is OrderSide.SELL
    assert intents[0].base_quantity == Decimal("2.5")


def test_adapter_clips_to_metadata_bounds_and_available_cash() -> None:
    capped = CappedWeightStrategy(Decimal("2"))
    negative = ConstantWeightStrategy(Decimal("-1"))

    buy = capped.on_bar(_context(cash="1000", quantity="0", close="100"))
    no_sell = negative.on_bar(
        _context(cash="1000", quantity="0", close="100")
    )

    assert buy[0].quote_amount == Decimal("750")
    assert no_sell == ()


def test_adapter_skips_deviation_inside_tolerance() -> None:
    strategy = ConstantWeightStrategy(
        Decimal("0.50"),
        tolerance=Decimal("0.02"),
    )

    intents = strategy.on_bar(
        _context(cash="510", quantity="4.9", close="100")
    )

    assert intents == ()


def test_adapter_never_sells_more_than_current_spot_quantity() -> None:
    strategy = ConstantWeightStrategy(Decimal("0"))

    intent = strategy.on_bar(
        _context(cash="0", quantity="1", close="100")
    )[0]

    assert intent.base_quantity == Decimal("1")


def test_strategy_metadata_rejects_invalid_weight_bounds() -> None:
    with pytest.raises(ValidationError, match="weight"):
        StrategyMetadata(
            id="invalid_weights",
            version="1",
            description="Invalid fixture.",
            warmup_bars=0,
            supported_timeframes=("1d",),
            min_weight=Decimal("0.8"),
            max_weight=Decimal("0.2"),
        )


@pytest.mark.parametrize("tolerance", [Decimal("-0.1"), Decimal("1.1")])
def test_adapter_rejects_invalid_tolerance(tolerance: Decimal) -> None:
    with pytest.raises(ValueError, match="tolerance"):
        ConstantWeightStrategy(Decimal("0.5"), tolerance=tolerance)
