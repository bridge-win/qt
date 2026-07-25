from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from btc_backtest.engine.fills import BarFillModel, FillPolicy
from btc_backtest.engine.models import (
    InstrumentKind,
    Order,
    OrderSide,
    OrderType,
)
from btc_backtest.errors import DataValidationError, ExecutionError
from hypothesis import given, settings
from hypothesis import strategies as st

UTC = timezone.utc


def ts(hour: int = 1) -> datetime:
    return datetime(2024, 1, 1, hour, tzinfo=UTC)


def order(
    *,
    side: OrderSide,
    order_type: OrderType,
    quantity: str = "1",
    limit: str | None = None,
    stop: str | None = None,
    target: str | None = None,
    expires_at: datetime | None = None,
) -> Order:
    return Order(
        id=f"{side.value}-{order_type.value}",
        created_at=ts(0),
        instrument=InstrumentKind.SPOT,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit) if limit is not None else None,
        stop_price=Decimal(stop) if stop is not None else None,
        take_profit_price=Decimal(target) if target is not None else None,
        expires_at=expires_at,
        reason="test",
    )


def bar(
    *,
    open: str = "100",
    high: str = "105",
    low: str = "95",
    close: str = "102",
) -> dict[str, Decimal]:
    return {
        "open": Decimal(open),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(close),
        "volume": Decimal("10"),
    }


def model(
    *,
    fee_bps: str = "0",
    slippage_bps: str = "0",
) -> BarFillModel:
    return BarFillModel(
        FillPolicy(
            fee_bps=Decimal(fee_bps),
            slippage_bps=Decimal(slippage_bps),
        )
    )


def test_buy_market_fill_applies_adverse_slippage_and_fee() -> None:
    result = model(fee_bps="10", slippage_bps="5").evaluate(
        order(side=OrderSide.BUY, order_type=OrderType.MARKET),
        bar(),
        ts(),
    )

    assert result is not None
    assert result.price == Decimal("100.05")
    assert result.fee == Decimal("0.10005")
    assert result.reason == "market"


def test_sell_market_fill_applies_adverse_slippage() -> None:
    result = model(slippage_bps="5").evaluate(
        order(side=OrderSide.SELL, order_type=OrderType.MARKET),
        bar(),
        ts(),
    )

    assert result is not None
    assert result.price == Decimal("99.95")


@pytest.mark.parametrize(
    ("candidate", "market_bar", "expected"),
    [
        (
            order(
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                limit="100",
            ),
            bar(open="98", high="101", low="97", close="100"),
            Decimal("98"),
        ),
        (
            order(
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                limit="100",
            ),
            bar(open="102", high="103", low="99", close="101"),
            Decimal("102"),
        ),
    ],
)
def test_crossed_limit_fills_no_worse_than_limit(
    candidate: Order,
    market_bar: dict[str, Decimal],
    expected: Decimal,
) -> None:
    result = model().evaluate(candidate, market_bar, ts())

    assert result is not None
    assert result.price == expected


@pytest.mark.parametrize(
    ("candidate", "market_bar", "expected"),
    [
        (
            order(
                side=OrderSide.SELL,
                order_type=OrderType.STOP,
                stop="95",
            ),
            bar(open="90", high="92", low="85", close="88"),
            Decimal("90"),
        ),
        (
            order(
                side=OrderSide.BUY,
                order_type=OrderType.STOP,
                stop="105",
            ),
            bar(open="110", high="115", low="108", close="112"),
            Decimal("110"),
        ),
    ],
)
def test_stop_gap_fills_at_worse_open(
    candidate: Order,
    market_bar: dict[str, Decimal],
    expected: Decimal,
) -> None:
    result = model().evaluate(candidate, market_bar, ts())

    assert result is not None
    assert result.price == expected


def test_stop_limit_requires_trigger_and_post_trigger_limit_touch() -> None:
    candidate = order(
        side=OrderSide.BUY,
        order_type=OrderType.STOP_LIMIT,
        stop="105",
        limit="103",
    )

    assert model().evaluate(
        candidate,
        bar(open="106", high="110", low="104", close="108"),
        ts(),
    ) is None

    result = model().evaluate(
        candidate,
        bar(open="106", high="110", low="102", close="108"),
        ts(),
    )
    assert result is not None
    assert result.price <= Decimal("103")
    assert result.reason == "stop_limit"


def test_expired_order_does_not_fill() -> None:
    candidate = order(
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        expires_at=ts(),
    )

    assert model().evaluate(candidate, bar(), ts()) is None


def test_order_cannot_fill_before_creation() -> None:
    candidate = order(
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
    )

    with pytest.raises(ExecutionError, match="creation"):
        model().evaluate(candidate, bar(), ts(0) - timedelta(seconds=1))


def test_adverse_first_stop_wins_when_target_and_stop_share_bar() -> None:
    candidate = order(
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        stop="90",
        target="110",
    )

    events = model().evaluate_bracket(
        candidate,
        bar(open="100", high="115", low="85", close="105"),
        ts(),
    )

    assert len(events) == 1
    assert events[0].reason == "stop"
    assert events[0].price <= Decimal("90")


def test_bracket_target_fills_when_stop_is_not_touched() -> None:
    candidate = order(
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        stop="90",
        target="110",
    )

    events = model().evaluate_bracket(
        candidate,
        bar(open="100", high="115", low="95", close="110"),
        ts(),
    )

    assert len(events) == 1
    assert events[0].reason == "target"
    assert events[0].price >= Decimal("110")


def test_fill_model_rejects_invalid_bar() -> None:
    with pytest.raises(DataValidationError, match="high"):
        model().evaluate(
            order(side=OrderSide.BUY, order_type=OrderType.MARKET),
            bar(open="100", high="99", low="95", close="102"),
            ts(),
        )


@given(
    open_price=st.decimals(
        min_value="1",
        max_value="1000000",
        places=4,
        allow_nan=False,
        allow_infinity=False,
    ),
    quantity=st.decimals(
        min_value="0.0001",
        max_value="100",
        places=4,
        allow_nan=False,
        allow_infinity=False,
    ),
    fee_bps=st.decimals(
        min_value="0",
        max_value="100",
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    slippage_bps=st.decimals(
        min_value="0",
        max_value="100",
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
)
@settings(max_examples=75, deadline=None)
def test_market_fill_cost_invariants(
    open_price: Decimal,
    quantity: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
) -> None:
    market_bar = bar(
        open=str(open_price),
        high=str(open_price),
        low=str(open_price),
        close=str(open_price),
    )
    fill_model = BarFillModel(
        FillPolicy(fee_bps=fee_bps, slippage_bps=slippage_bps)
    )
    buy = fill_model.evaluate(
        order(
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=str(quantity),
        ),
        market_bar,
        ts(),
    )
    sell = fill_model.evaluate(
        order(
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=str(quantity),
        ),
        market_bar,
        ts(),
    )

    assert buy is not None and sell is not None
    assert buy.price >= open_price
    assert sell.price <= open_price
    assert buy.fee >= 0 and sell.fee >= 0
    assert buy.quantity == sell.quantity == quantity
