"""Gaps, receipt ordering, stale prices and future data cannot become signals."""

from __future__ import annotations

import pytest

from qt.marketdata import (
    BookInvalidError,
    ClosedBar,
    DepthDelta,
    DepthSnapshot,
    OrderBook,
    PriceLevel,
    asof_available,
    closed_bars_asof,
    latest_closed_bar,
)


def level(price="100", quantity="1") -> PriceLevel:
    return PriceLevel(price=price, quantity=quantity)


def snapshot(sequence=100, received=100) -> DepthSnapshot:
    return DepthSnapshot(last_update_id=sequence, bids=(level(),), asks=(level("102"),),
                         exchange_ts_ns=received - 1, received_ts_ns=received)


def delta(first=101, last=101, received=101, **changes) -> DepthDelta:
    return DepthDelta(first_update_id=first, last_update_id=last, exchange_ts_ns=received - 1,
                      received_ts_ns=received, **changes)


def valid_book() -> OrderBook:
    book = OrderBook()
    assert book.apply_snapshot(snapshot())
    assert not book.is_valid
    assert book.apply_delta(delta())
    return book


def test_initial_snapshot_requires_a_bridging_delta() -> None:
    book = OrderBook()
    assert not book.apply_delta(delta())
    book.apply_snapshot(snapshot())
    assert not book.apply_delta(delta(first=98, last=100))
    with pytest.raises(BookInvalidError, match="bridging"):
        book.view(now_ns=102, maximum_age_ns=10)
    assert book.apply_delta(delta(first=99, last=102, received=102))
    assert book.view(now_ns=103, maximum_age_ns=10).last_update_id == 102


def test_gap_permanently_invalidates_until_explicit_resync() -> None:
    book = valid_book()
    assert not book.apply_delta(delta(first=103, last=104, received=104))
    assert book.invalid_reason == "sequence_gap"
    assert not book.apply_delta(delta(first=102, last=102, received=105))
    assert not book.is_valid
    with pytest.raises(BookInvalidError):
        book.view(now_ns=105, maximum_age_ns=10)
    book.apply_snapshot(snapshot(sequence=110, received=110))
    assert book.apply_delta(delta(first=111, last=111, received=111))
    assert book.is_valid


def test_levels_update_delete_and_preserve_last_valid_state_on_cross() -> None:
    book = valid_book()
    assert book.apply_delta(delta(first=102, last=102, received=102,
                                  bids=(level("100", "0"), level("101", "2"))))
    view = book.view(now_ns=103, maximum_age_ns=10)
    assert view.bids[0].price == 101
    assert view.bids[0].quantity == 2
    assert not book.apply_delta(delta(first=103, last=103, received=103,
                                      bids=(level("103", "1"),)))
    assert book.invalid_reason == "crossed_or_locked_book"


def test_exact_duplicate_is_ignored_conflicting_duplicate_invalidates() -> None:
    book = valid_book()
    assert not book.apply_delta(delta())
    assert book.is_valid
    assert not book.apply_delta(delta(bids=(level(quantity="2"),)))
    assert not book.is_valid


def test_out_of_order_delta_and_future_sequence_cannot_be_sorted_away() -> None:
    book = valid_book()
    assert book.apply_delta(delta(first=102, last=102, received=102))
    assert not book.apply_delta(delta())
    assert book.invalid_reason == "out_of_order_or_conflicting_duplicate"


def test_futures_previous_final_id_and_receipt_time_checked() -> None:
    book = valid_book()
    assert not book.apply_delta(delta(first=102, last=102, received=102, previous_final_update_id=99))
    assert book.invalid_reason == "previous_sequence_mismatch"
    book = valid_book()
    assert not book.apply_delta(delta(first=102, last=102, received=100))
    assert book.invalid_reason == "receipt_time_regression"


def test_binance_bootstrap_accepts_ordered_buffered_deltas_before_rest_receipt() -> None:
    book = OrderBook()
    book.apply_snapshot(snapshot(sequence=100, received=110))
    assert book.apply_delta(delta(first=99, last=101, received=105))
    assert book.apply_delta(delta(first=102, last=102, received=106))
    assert book.view(now_ns=111, maximum_age_ns=20).last_update_id == 102


def test_book_depth_bound_invalidates_instead_of_silently_dropping_levels() -> None:
    book = OrderBook(maximum_levels_per_side=1)
    book.apply_snapshot(snapshot())
    assert book.apply_delta(delta())
    assert not book.apply_delta(delta(first=102, last=102, received=102,
                                      bids=(level("99", "1"),)))
    assert book.invalid_reason == "depth_capacity_requires_resnapshot"


def test_stale_exchange_backlog_and_local_clock_regression_block_decision() -> None:
    book = valid_book()
    with pytest.raises(BookInvalidError, match="stale"):
        book.view(now_ns=120, maximum_age_ns=10)
    with pytest.raises(BookInvalidError, match="future"):
        book.view(now_ns=100, maximum_age_ns=10)
    assert not book.is_valid
    book = OrderBook()
    book.apply_snapshot(snapshot())
    book.apply_delta(DepthDelta(first_update_id=101, last_update_id=101,
                                exchange_ts_ns=1, received_ts_ns=101))
    with pytest.raises(BookInvalidError, match="stale_exchange"):
        book.view(now_ns=102, maximum_age_ns=10)


def bar(open_at, close_price="101") -> ClosedBar:
    return ClosedBar(instrument="BTCUSDT", open_ts_ns=open_at, close_ts_ns=open_at + 10,
                     available_at_ns=open_at + 11, received_at_ns=open_at + 12,
                     open="100", high="200", low="90", close=close_price, volume="1")


def test_multiframe_only_exposes_closed_received_bars() -> None:
    bars = (bar(0), bar(10), bar(20))
    assert latest_closed_bar(bars, decision_at_ns=10) is None
    assert latest_closed_bar(bars, decision_at_ns=11, use_received_time=False) == bars[0]
    assert latest_closed_bar(bars, decision_at_ns=21) == bars[0]
    assert latest_closed_bar(bars, decision_at_ns=22) == bars[1]
    assert latest_closed_bar(bars, decision_at_ns=100, maximum_age_ns=10) is None


def test_future_mutation_and_prefix_extension_do_not_change_past_features() -> None:
    prefix = (bar(0), bar(10))
    historical = closed_bars_asof(prefix, decision_at_ns=22)
    future_a = (*prefix, bar(20, "199"), bar(30, "95"))
    future_b = (*prefix, bar(20, "91"), bar(30, "198"))
    assert historical == closed_bars_asof(future_a, decision_at_ns=22)
    assert historical == closed_bars_asof(future_b, decision_at_ns=22)
    assert asof_available(((10, "a"), (20, "b"), (30, "c")), decision_at_ns=20) == "b"
    assert asof_available(((10, "a"), (20, "b"), (30, "changed")), decision_at_ns=20) == "b"


def test_out_of_order_visible_bars_or_observations_are_rejected() -> None:
    with pytest.raises(ValueError, match="order"):
        closed_bars_asof((bar(10), bar(0)), decision_at_ns=100)
    with pytest.raises(ValueError, match="order"):
        asof_available(((20, "b"), (10, "a")), decision_at_ns=100)
    assert asof_available(((10, "a"),), decision_at_ns=20, maximum_age_ns=5) is None
