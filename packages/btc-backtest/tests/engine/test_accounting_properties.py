from datetime import datetime, timedelta, timezone
from decimal import Decimal

from btc_backtest.engine.accounting import Portfolio
from btc_backtest.engine.models import Fill, InstrumentKind, OrderSide
from hypothesis import given, settings
from hypothesis import strategies as st

UTC = timezone.utc


def event(
    index: int,
    side: str,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    instrument: InstrumentKind = InstrumentKind.SPOT,
) -> Fill:
    timestamp = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
    return Fill(
        id=f"fill-{index}",
        order_id=f"order-{index}",
        order_created_at=timestamp - timedelta(seconds=1),
        timestamp=timestamp,
        instrument=instrument,
        side=OrderSide(side),
        quantity=quantity,
        price=price,
        fee=fee,
        reason="property",
        intrabar_policy="adverse_first",
    )


@given(
    trades=st.lists(
        st.tuples(
            st.decimals(
                min_value="1",
                max_value="100000",
                places=2,
                allow_nan=False,
                allow_infinity=False,
            ),
            st.decimals(
                min_value="0.0001",
                max_value="10",
                places=4,
                allow_nan=False,
                allow_infinity=False,
            ),
            st.decimals(
                min_value="0",
                max_value="10",
                places=4,
                allow_nan=False,
                allow_infinity=False,
            ),
        ),
        min_size=1,
        max_size=30,
    )
)
@settings(max_examples=75, deadline=None)
def test_spot_round_trip_accounting_invariants(
    trades: list[tuple[Decimal, Decimal, Decimal]],
) -> None:
    portfolio = Portfolio(Decimal("1000000000"))
    event_index = 1

    for price, quantity, fee in trades:
        portfolio.apply_fill(
            event(event_index, "buy", quantity, price, fee)
        )
        event_index += 1
        snapshot = portfolio.apply_fill(
            event(event_index, "sell", quantity, price, fee)
        )
        event_index += 1

        assert snapshot.cash.is_finite()
        assert snapshot.equity.is_finite()
        assert snapshot.realized_pnl.is_finite()
        assert snapshot.position("spot").quantity >= 0
        assert snapshot.cash == snapshot.equity
        assert snapshot.cash == Decimal("1000000000") + snapshot.realized_pnl


@given(
    trades=st.lists(
        st.tuples(
            st.decimals(
                min_value="1",
                max_value="100000",
                places=2,
                allow_nan=False,
                allow_infinity=False,
            ),
            st.decimals(
                min_value="1",
                max_value="100000",
                places=2,
                allow_nan=False,
                allow_infinity=False,
            ),
            st.decimals(
                min_value="0.0001",
                max_value="10",
                places=4,
                allow_nan=False,
                allow_infinity=False,
            ),
            st.decimals(
                min_value="0",
                max_value="10",
                places=4,
                allow_nan=False,
                allow_infinity=False,
            ),
        ),
        min_size=1,
        max_size=30,
    )
)
@settings(max_examples=75, deadline=None)
def test_perpetual_short_round_trip_accounting_invariants(
    trades: list[tuple[Decimal, Decimal, Decimal, Decimal]],
) -> None:
    initial_cash = Decimal("1000000000")
    portfolio = Portfolio(initial_cash)
    event_index = 1

    for entry, exit_price, quantity, fee in trades:
        portfolio.apply_fill(
            event(
                event_index,
                "sell",
                quantity,
                entry,
                fee,
                InstrumentKind.PERPETUAL,
            )
        )
        event_index += 1
        snapshot = portfolio.apply_fill(
            event(
                event_index,
                "buy",
                quantity,
                exit_price,
                fee,
                InstrumentKind.PERPETUAL,
            )
        )
        event_index += 1

        assert snapshot.position("perpetual").quantity == 0
        assert snapshot.cash == snapshot.equity
        assert snapshot.cash == initial_cash + snapshot.realized_pnl
