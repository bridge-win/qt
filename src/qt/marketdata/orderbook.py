"""Deterministic L2 reconstruction with explicit invalidation and resync.

Binance spot ranges must bridge ``last_update_id + 1``. Futures can also
provide ``previous_final_update_id``. Input is processed in receipt order;
sorting deltas cannot conceal missing or out-of-order updates.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from qt.contracts import Contract
from qt.contracts.base import NonNegativeDecimal, PositiveDecimal, TimestampNS


class BookInvalidError(RuntimeError):
    """Decision-making requires a valid, current, fully synchronized book."""


class PriceLevel(Contract):
    price: PositiveDecimal
    quantity: NonNegativeDecimal


class DepthSnapshot(Contract):
    last_update_id: int = Field(ge=0)
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    exchange_ts_ns: TimestampNS
    received_ts_ns: TimestampNS


class DepthDelta(Contract):
    first_update_id: int = Field(ge=0)
    last_update_id: int = Field(ge=0)
    previous_final_update_id: int | None = Field(default=None, ge=0)
    bids: tuple[PriceLevel, ...] = ()
    asks: tuple[PriceLevel, ...] = ()
    exchange_ts_ns: TimestampNS
    received_ts_ns: TimestampNS

    @model_validator(mode="after")
    def ordered_range(self) -> DepthDelta:
        if self.last_update_id < self.first_update_id:
            raise ValueError("delta sequence range is reversed")
        return self


class BookView(Contract):
    last_update_id: int = Field(ge=0)
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    exchange_ts_ns: TimestampNS
    received_ts_ns: TimestampNS


class OrderBook:
    """Single-writer L2 state. A snapshot needs a bridging delta before use.

    A gap permanently invalidates this instance's current reconstruction.
    Only an explicit ``apply_snapshot`` can start synchronization again.
    Exact duplicate deltas are harmless; conflicting duplicates and late
    deltas after the bridge invalidate the book.
    """

    def __init__(self, *, maximum_levels_per_side: int = 10_000) -> None:
        if type(maximum_levels_per_side) is not int or maximum_levels_per_side < 1:
            raise ValueError("maximum levels must be a positive integer")
        self.maximum_levels_per_side = maximum_levels_per_side
        self.status: Literal["INVALID", "SYNCING", "VALID"] = "INVALID"
        self.invalid_reason: str | None = "snapshot_required"
        self.last_update_id: int | None = None
        self.exchange_ts_ns = 0
        self.received_ts_ns = 0
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._last_delta_hash: str | None = None
        self._last_delta_received_ns: int | None = None

    @property
    def is_valid(self) -> bool:
        return self.status == "VALID"

    def invalidate(self, reason: str) -> None:
        self.status = "INVALID"
        self.invalid_reason = reason

    @staticmethod
    def _levels(levels: tuple[PriceLevel, ...], *, snapshot: bool) -> dict[Decimal, Decimal]:
        if len({level.price for level in levels}) != len(levels):
            raise BookInvalidError("duplicate_price_level")
        if snapshot and any(level.quantity == 0 for level in levels):
            raise BookInvalidError("zero_quantity_in_snapshot")
        return {level.price: level.quantity for level in levels}

    @staticmethod
    def _check_spread(bids: dict[Decimal, Decimal], asks: dict[Decimal, Decimal]) -> None:
        if not bids or not asks:
            raise BookInvalidError("empty_side")
        if max(bids) >= min(asks):
            raise BookInvalidError("crossed_or_locked_book")

    def apply_snapshot(self, snapshot: DepthSnapshot) -> bool:
        try:
            bids = self._levels(snapshot.bids, snapshot=True)
            asks = self._levels(snapshot.asks, snapshot=True)
            if max(len(bids), len(asks)) > self.maximum_levels_per_side:
                raise BookInvalidError("depth_capacity_requires_resnapshot")
            self._check_spread(bids, asks)
        except BookInvalidError as exc:
            self.invalidate(str(exc))
            return False
        self._bids, self._asks = bids, asks
        self.last_update_id = snapshot.last_update_id
        self.exchange_ts_ns = snapshot.exchange_ts_ns
        self.received_ts_ns = snapshot.received_ts_ns
        self._last_delta_hash = None
        self._last_delta_received_ns = None
        self.status, self.invalid_reason = "SYNCING", "bridging_delta_required"
        return True

    def apply_delta(self, delta: DepthDelta) -> bool:
        """Return True only if a new delta was applied; never auto-recover a gap."""
        if self.status == "INVALID" or self.last_update_id is None:
            return False
        digest = delta.model_copy(update={"received_ts_ns": 0}).content_hash()
        if self.status == "SYNCING" and delta.last_update_id <= self.last_update_id:
            return False  # old websocket frames queued before the REST snapshot
        if delta.last_update_id == self.last_update_id and digest == self._last_delta_hash:
            return False
        if delta.last_update_id <= self.last_update_id:
            self.invalidate("out_of_order_or_conflicting_duplicate")
            return False
        if (self._last_delta_received_ns is not None
                and delta.received_ts_ns < self._last_delta_received_ns):
            self.invalidate("receipt_time_regression")
            return False
        if not delta.first_update_id <= self.last_update_id + 1 <= delta.last_update_id:
            self.invalidate("sequence_gap")
            return False
        if (self.status == "VALID" and delta.previous_final_update_id is not None
                and delta.previous_final_update_id != self.last_update_id):
            self.invalidate("previous_sequence_mismatch")
            return False
        try:
            bid_changes = self._levels(delta.bids, snapshot=False)
            ask_changes = self._levels(delta.asks, snapshot=False)
            bids, asks = self._bids.copy(), self._asks.copy()
            for target, changes in ((bids, bid_changes), (asks, ask_changes)):
                for price, quantity in changes.items():
                    if quantity == 0:
                        target.pop(price, None)
                    else:
                        target[price] = quantity
            if max(len(bids), len(asks)) > self.maximum_levels_per_side:
                raise BookInvalidError("depth_capacity_requires_resnapshot")
            self._check_spread(bids, asks)
        except BookInvalidError as exc:
            self.invalidate(str(exc))
            return False
        self._bids, self._asks = bids, asks
        self.last_update_id = delta.last_update_id
        self.exchange_ts_ns = delta.exchange_ts_ns
        # During Binance bootstrap, buffered websocket deltas can have arrived
        # before the REST snapshot response. Compare delta receipt order against
        # other deltas, not against that later REST receipt timestamp.
        self.received_ts_ns = max(self.received_ts_ns, delta.received_ts_ns)
        self._last_delta_received_ns = delta.received_ts_ns
        self._last_delta_hash = digest
        self.status, self.invalid_reason = "VALID", None
        return True

    def view(self, *, now_ns: int, maximum_age_ns: int, depth: int = 20) -> BookView:
        if type(now_ns) is not int or type(maximum_age_ns) is not int:
            raise ValueError("book clock and age must be integer nanoseconds")
        if type(depth) is not int or depth <= 0 or maximum_age_ns < 0 or now_ns < 0:
            raise ValueError("depth must be positive and age nonnegative")
        if not self.is_valid or self.last_update_id is None:
            raise BookInvalidError(self.invalid_reason or "invalid_book")
        if now_ns < self.received_ts_ns:
            self.invalidate("receipt_in_future")
            raise BookInvalidError("receipt_in_future")
        if now_ns < self.exchange_ts_ns:
            self.invalidate("exchange_time_in_future")
            raise BookInvalidError("exchange_time_in_future")
        if now_ns - self.received_ts_ns > maximum_age_ns:
            raise BookInvalidError("stale_book")
        if now_ns - self.exchange_ts_ns > maximum_age_ns:
            raise BookInvalidError("stale_exchange_data")
        return BookView(
            last_update_id=self.last_update_id,
            bids=tuple(PriceLevel(price=price, quantity=quantity)
                       for price, quantity in sorted(self._bids.items(), reverse=True)[:depth]),
            asks=tuple(PriceLevel(price=price, quantity=quantity)
                       for price, quantity in sorted(self._asks.items())[:depth]),
            exchange_ts_ns=self.exchange_ts_ns, received_ts_ns=self.received_ts_ns,
        )
