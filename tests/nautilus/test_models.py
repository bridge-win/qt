from __future__ import annotations

from decimal import Decimal

import pytest

from qt.nautilus.models import DataRequirement, ExecutionAssumptions


def test_queue_assumption_requires_book_evidence() -> None:
    assumptions = ExecutionAssumptions(initial_cash=Decimal("100"), queue_position=True)
    assert assumptions.data_requirement is DataRequirement.ORDER_BOOK


def test_research_leverage_defaults_to_one_and_allows_bounded_margin_studies() -> None:
    assert ExecutionAssumptions(initial_cash=Decimal("100")).leverage == Decimal("1")
    assert ExecutionAssumptions(
        initial_cash=Decimal("100"), leverage=Decimal("2")
    ).leverage == Decimal("2")
    with pytest.raises(ValueError, match="3x"):
        ExecutionAssumptions(initial_cash=Decimal("100"), leverage=Decimal("4"))


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_native_decimal_assumptions_reject_non_finite_values(value: Decimal) -> None:
    with pytest.raises(ValueError, match="finite"):
        ExecutionAssumptions(initial_cash=value)


def test_fixed_bps_slippage_and_synthetic_funding_are_rejected() -> None:
    with pytest.raises(ValueError, match="slippage_bps"):
        ExecutionAssumptions(initial_cash=Decimal("100"), slippage_bps=Decimal("5"))
    with pytest.raises(ValueError, match="funding"):
        ExecutionAssumptions(initial_cash=Decimal("100"), funding_bps=Decimal("1"))
