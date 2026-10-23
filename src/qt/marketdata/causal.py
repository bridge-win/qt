"""Causal multi-timeframe access without silently sorting or filling gaps."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from pydantic import model_validator

from qt.contracts import Contract
from qt.contracts.base import NonNegativeDecimal, PositiveDecimal, TimestampNS


class ClosedBar(Contract):
    instrument: str
    open_ts_ns: TimestampNS
    close_ts_ns: TimestampNS
    available_at_ns: TimestampNS
    received_at_ns: TimestampNS
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeDecimal

    @model_validator(mode="after")
    def valid_bar(self) -> ClosedBar:
        if self.close_ts_ns <= self.open_ts_ns:
            raise ValueError("bar must close after it opens")
        if self.available_at_ns < self.close_ts_ns or self.received_at_ns < self.available_at_ns:
            raise ValueError("a closed bar cannot be available before closing")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("bar OHLC prices are inconsistent")
        return self


def closed_bars_asof(
    bars: Sequence[ClosedBar], *, decision_at_ns: int, use_received_time: bool = True,
) -> tuple[ClosedBar, ...]:
    """Expose only fully closed, actually available bars in their source order.

    Validation is limited to the observable prefix. A future row, including a
    later revision or malformed future ordering, cannot change past signals.
    The sequence must be supplied in availability order by the dataset reader.
    """
    if type(decision_at_ns) is not int or decision_at_ns < 0:
        raise ValueError("decision time must be nonnegative integer nanoseconds")
    selected: list[ClosedBar] = []
    previous_availability: int | None = None
    for bar in bars:
        known_at = bar.received_at_ns if use_received_time else bar.available_at_ns
        if known_at > decision_at_ns:
            break
        if previous_availability is not None and known_at < previous_availability:
            raise ValueError("bars are out of availability order")
        if selected and bar.open_ts_ns < selected[-1].close_ts_ns:
            raise ValueError("bars overlap or revise an earlier bar; use an explicit revision stream")
        previous_availability = known_at
        if bar.close_ts_ns <= decision_at_ns:
            selected.append(bar)
    return tuple(selected)


def latest_closed_bar(
    bars: Sequence[ClosedBar], *, decision_at_ns: int, use_received_time: bool = True,
    maximum_age_ns: int | None = None,
) -> ClosedBar | None:
    if maximum_age_ns is not None and (type(maximum_age_ns) is not int or maximum_age_ns < 0):
        raise ValueError("maximum age must be nonnegative integer nanoseconds")
    available = closed_bars_asof(bars, decision_at_ns=decision_at_ns,
                                use_received_time=use_received_time)
    if not available:
        return None
    latest = available[-1]
    if maximum_age_ns is not None and decision_at_ns - latest.close_ts_ns > maximum_age_ns:
        return None
    return latest


T = TypeVar("T")


def asof_available(
    observations: Sequence[tuple[int, T]], *, decision_at_ns: int,
    maximum_age_ns: int | None = None,
) -> T | None:
    """Last point-in-time observation, with an optional explicit stale cutoff."""
    if type(decision_at_ns) is not int or decision_at_ns < 0:
        raise ValueError("decision time must be nonnegative integer nanoseconds")
    if maximum_age_ns is not None and (type(maximum_age_ns) is not int or maximum_age_ns < 0):
        raise ValueError("maximum age must be nonnegative integer nanoseconds")
    latest: tuple[int, T] | None = None
    for available_at_ns, value in observations:
        if type(available_at_ns) is not int or available_at_ns < 0:
            raise ValueError("observation availability must be nonnegative integer nanoseconds")
        if available_at_ns > decision_at_ns:
            break
        if latest is not None and available_at_ns < latest[0]:
            raise ValueError("observations are out of availability order")
        latest = (available_at_ns, value)
    if latest is None:
        return None
    if maximum_age_ns is not None and decision_at_ns - latest[0] > maximum_age_ns:
        return None
    return latest[1]
