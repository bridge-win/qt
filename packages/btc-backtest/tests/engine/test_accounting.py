from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from btc_backtest.engine.accounting import Portfolio
from btc_backtest.engine.models import (
    Fill,
    FundingEvent,
    InstrumentKind,
    OrderSide,
)
from btc_backtest.errors import ExecutionError
from pydantic import ValidationError

UTC = timezone.utc


def ts(hour: int) -> datetime:
    return datetime(2024, 1, 1, hour, tzinfo=UTC)


def fill(
    side: str,
    *,
    fill_id: str,
    qty: str = "1",
    price: str = "100",
    fee: str = "1",
    instrument: str = "spot",
    hour: int = 1,
) -> Fill:
    return Fill(
        id=fill_id,
        order_id=f"order-{fill_id}",
        order_created_at=ts(hour) - timedelta(minutes=1),
        timestamp=ts(hour),
        instrument=InstrumentKind(instrument),
        side=OrderSide(side),
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        reason="test",
        intrabar_policy="adverse_first",
    )


def test_buy_then_sell_reconciles_cash_equity_and_realized_pnl() -> None:
    portfolio = Portfolio(Decimal("10000"))
    portfolio.apply_fill(fill("buy", fill_id="buy"))

    opened = portfolio.mark(ts(1), Decimal("110"))

    assert opened.cash == Decimal("9899")
    assert opened.equity == Decimal("10009")
    assert opened.unrealized_pnl == Decimal("10")

    portfolio.apply_fill(
        fill(
            "sell",
            fill_id="sell",
            price="110",
            hour=2,
        )
    )
    closed = portfolio.mark(ts(2), Decimal("110"))

    assert closed.cash == closed.equity == Decimal("10008")
    assert closed.realized_pnl == Decimal("8")
    assert closed.position("spot").quantity == 0


def test_spot_buy_cannot_spend_unavailable_cash() -> None:
    portfolio = Portfolio(Decimal("100"))

    with pytest.raises(ExecutionError, match="cash"):
        portfolio.apply_fill(
            fill(
                "buy",
                fill_id="too-large",
                qty="1",
                price="100",
                fee="1",
            )
        )

    assert portfolio.mark(ts(1), Decimal("100")).cash == Decimal("100")


def test_spot_sell_cannot_exceed_holdings() -> None:
    portfolio = Portfolio(Decimal("1000"))
    portfolio.apply_fill(
        fill(
            "buy",
            fill_id="buy",
            qty="1",
            fee="0",
        )
    )

    with pytest.raises(ExecutionError, match="holdings"):
        portfolio.apply_fill(
            fill(
                "sell",
                fill_id="oversell",
                qty="2",
                fee="0",
                hour=2,
            )
        )


def test_duplicate_fill_id_is_rejected_without_double_charging() -> None:
    portfolio = Portfolio(Decimal("1000"))
    event = fill("buy", fill_id="same", fee="1")
    first = portfolio.apply_fill(event)

    with pytest.raises(ExecutionError, match="duplicate fill"):
        portfolio.apply_fill(event)

    assert portfolio.mark(ts(1), Decimal("100")).cash == first.cash


def test_fill_cannot_precede_order_creation() -> None:
    with pytest.raises(ValidationError, match="creation"):
        Fill(
            id="bad",
            order_id="order-bad",
            order_created_at=ts(2),
            timestamp=ts(1),
            instrument=InstrumentKind.SPOT,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0"),
            reason="test",
            intrabar_policy="adverse_first",
        )


def test_perpetual_short_marks_closes_and_applies_funding_once() -> None:
    portfolio = Portfolio(Decimal("10000"))
    portfolio.apply_fill(
        fill(
            "sell",
            fill_id="short",
            instrument="perpetual",
            fee="1",
        )
    )

    marked = portfolio.mark(
        ts(1),
        {"spot": Decimal("100"), "perpetual": Decimal("90")},
    )
    assert marked.unrealized_pnl == Decimal("10")
    assert marked.equity == Decimal("10009")

    funding = FundingEvent(
        id="funding-1",
        timestamp=ts(2),
        instrument=InstrumentKind.PERPETUAL,
        amount=Decimal("2"),
        rate=Decimal("0.0002"),
    )
    funded = portfolio.apply_funding(funding)
    assert funded.cash == marked.cash + Decimal("2")

    with pytest.raises(ExecutionError, match="duplicate funding"):
        portfolio.apply_funding(funding)

    closed = portfolio.apply_fill(
        fill(
            "buy",
            fill_id="cover",
            instrument="perpetual",
            price="90",
            fee="1",
            hour=3,
        )
    )
    assert closed.position("perpetual").quantity == 0
    assert closed.realized_pnl == Decimal("10")
    assert closed.cash == Decimal("10010")


def test_directional_long_perpetual_is_rejected() -> None:
    with pytest.raises(ExecutionError, match="long perpetual"):
        Portfolio(Decimal("10000")).apply_fill(
            fill(
                "buy",
                fill_id="long",
                instrument="perpetual",
                fee="0",
            )
        )


def test_marked_equity_combines_spot_value_and_perpetual_pnl() -> None:
    portfolio = Portfolio(Decimal("10000"))
    portfolio.apply_fill(
        fill(
            "buy",
            fill_id="spot",
            fee="0",
        )
    )
    portfolio.apply_fill(
        fill(
            "sell",
            fill_id="perp",
            instrument="perpetual",
            fee="0",
        )
    )

    snapshot = portfolio.mark(
        ts(2),
        {"spot": Decimal("110"), "perpetual": Decimal("90")},
    )

    assert snapshot.cash == Decimal("9900")
    assert snapshot.unrealized_pnl == Decimal("20")
    assert snapshot.equity == Decimal("10020")
