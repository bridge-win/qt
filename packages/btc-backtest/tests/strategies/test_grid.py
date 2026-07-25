from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import (
    InstrumentKind,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.strategies.base import StrategyContext
from btc_backtest.strategies.grid import GridParams, GridRebalance
from btc_backtest.strategies.registry import default_strategy_registry
from pydantic import ValidationError

UTC = timezone.utc


def _context(
    *,
    close: str,
    cash: str = "1000",
    quantity: str = "0",
    open_orders: tuple[Order, ...] = (),
) -> StrategyContext:
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    price = float(close)
    frame = pd.DataFrame(
        {
            "open": [price],
            "high": [price],
            "low": [price],
            "close": [price],
            "volume": [10.0],
        },
        index=pd.DatetimeIndex([timestamp]),
    )
    qty = Decimal(quantity)
    equity = Decimal(cash) + qty * Decimal(close)
    return StrategyContext(
        timestamp=timestamp,
        bars=frame,
        open_orders=open_orders,
        portfolio=PortfolioSnapshot(
            timestamp=timestamp,
            cash=Decimal(cash),
            equity=equity,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(
                Position(
                    instrument=InstrumentKind.SPOT,
                    quantity=qty,
                    average_price=Decimal(close) if qty > 0 else Decimal("0"),
                ),
                Position(instrument=InstrumentKind.PERPETUAL),
            ),
        ),
    )


def test_grid_places_levels_only_inside_configured_range() -> None:
    strategy = GridRebalance({"lower": 80, "upper": 120, "levels": 5, "quote_per_level": 100})

    intents = strategy.on_bar(_context(close="100", quantity="10"))

    assert {item.limit_price for item in intents} == {
        Decimal("80"),
        Decimal("90"),
        Decimal("110"),
        Decimal("120"),
    }


def test_grid_never_sells_more_than_spot_inventory() -> None:
    strategy = GridRebalance({"lower": 80, "upper": 120, "levels": 5, "quote_per_level": 100})

    intents = strategy.on_bar(_context(close="110", quantity="0.1"))
    sells = [item.base_quantity for item in intents if item.side is OrderSide.SELL]

    assert all(quantity is not None for quantity in sells)
    assert sum((quantity or Decimal("0") for quantity in sells), Decimal("0")) <= Decimal("0.1")


def test_grid_caps_buys_by_cash_and_max_inventory_weight() -> None:
    strategy = GridRebalance(
        {
            "lower": 80,
            "upper": 120,
            "levels": 5,
            "quote_per_level": 100,
            "max_inventory_weight": 0.25,
        }
    )

    intents = strategy.on_bar(_context(close="100", cash="1000"))
    total = sum(
        (item.quote_amount or Decimal("0") for item in intents if item.side is OrderSide.BUY),
        Decimal("0"),
    )

    assert total <= Decimal("250")


def test_grid_does_not_duplicate_an_existing_level() -> None:
    strategy = GridRebalance({"lower": 80, "upper": 120, "levels": 5, "quote_per_level": 100})
    existing = Order(
        id="existing",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("90"),
        status=OrderStatus.OPEN,
        group_id="grid:90",
        reason="grid_buy_level",
    )

    intents = strategy.on_bar(_context(close="100", quantity="10", open_orders=(existing,)))

    assert Decimal("90") not in {item.limit_price for item in intents}


@pytest.mark.parametrize(
    "parameters",
    [
        {"lower": 120, "upper": 80},
        {"levels": 1},
        {"levels": 101},
        {"quote_per_level": 0},
        {"max_inventory_weight": 1.1},
    ],
)
def test_grid_parameter_bounds(parameters: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GridParams.model_validate(parameters)


def test_grid_strategy_is_registered() -> None:
    assert isinstance(
        default_strategy_registry().create("grid_rebalance", {}),
        GridRebalance,
    )
