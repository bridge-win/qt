from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from btc_backtest.signals.ranking import SignalAggregator
from hypothesis import given
from hypothesis import strategies as st

from .helpers import observation

NOW = datetime(2024, 1, 3, tzinfo=timezone.utc)


@given(
    st.lists(
        st.tuples(
            st.decimals(
                min_value=Decimal("-1"),
                max_value=Decimal("1"),
                allow_nan=False,
                allow_infinity=False,
                places=4,
            ),
            st.decimals(
                min_value=Decimal("0"),
                max_value=Decimal("1"),
                allow_nan=False,
                allow_infinity=False,
                places=4,
            ),
        ),
        min_size=1,
        max_size=20,
    )
)
def test_ranked_direction_and_confidence_are_bounded(
    values: list[tuple[Decimal, Decimal]],
) -> None:
    items = tuple(
        observation(
            id=f"p{index}:e{index}",
            source_event_id=f"e{index}",
            provider=f"p{index}",
            direction=direction,
            confidence=confidence,
            effective_at=NOW - timedelta(hours=1),
            observed_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )
        for index, (direction, confidence) in enumerate(values)
    )

    for ranked in SignalAggregator().rank(items, as_of=NOW):
        assert Decimal("-1") <= ranked.direction <= Decimal("1")
        assert Decimal("0") <= ranked.confidence <= Decimal("1")
