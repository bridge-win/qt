# Migrated verbatim source operations helper.
from dataclasses import dataclass


@dataclass(frozen=True)
class OpenTrade:
    pair: str
    amount: float


@dataclass(frozen=True)
class ReconciliationResult:
    expected_amount: float
    exchange_amount: float
    difference: float
    within_tolerance: bool


def expected_spot_amount(pair: str, open_trades: list[OpenTrade]) -> float:
    return sum(trade.amount for trade in open_trades if trade.pair == pair)


def reconcile_spot_position(
    *,
    pair: str,
    open_trades: list[OpenTrade],
    exchange_free_amount: float,
    exchange_used_amount: float,
    tolerance: float,
) -> ReconciliationResult:
    if exchange_free_amount < 0:
        raise ValueError("exchange_free_amount cannot be negative")
    if exchange_used_amount < 0:
        raise ValueError("exchange_used_amount cannot be negative")
    if tolerance < 0:
        raise ValueError("tolerance cannot be negative")

    expected = expected_spot_amount(pair, open_trades)
    exchange_amount = exchange_free_amount + exchange_used_amount
    difference = exchange_amount - expected

    return ReconciliationResult(
        expected_amount=expected,
        exchange_amount=exchange_amount,
        difference=difference,
        within_tolerance=abs(difference) <= tolerance,
    )
