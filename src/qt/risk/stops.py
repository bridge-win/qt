"""Exit logic: ATR-based stops, take-profits, time stops."""

from __future__ import annotations

from datetime import datetime, timedelta


def atr_stop_price(entry_price: float, atr_value: float, mult: float = 2.5,
                   side: str = "long") -> float:
    if side == "long":
        return entry_price - mult * atr_value
    return entry_price + mult * atr_value


def atr_take_profit(entry_price: float, atr_value: float, mult: float = 4.0,
                    side: str = "long") -> float:
    if side == "long":
        return entry_price + mult * atr_value
    return entry_price - mult * atr_value


def time_stop_deadline(entry_ts: datetime, bars: int, bar_seconds: int) -> datetime:
    return entry_ts + timedelta(seconds=bars * bar_seconds)


def should_exit(
    side: str,
    mark_price: float,
    stop_price: float | None,
    take_profit_price: float | None,
    now: datetime,
    time_stop_ts: datetime | None,
) -> str | None:
    """Return a reason string when an exit should fire, else None."""

    if side == "long":
        if stop_price is not None and mark_price <= stop_price:
            return "stop_loss"
        if take_profit_price is not None and mark_price >= take_profit_price:
            return "take_profit"
    else:
        if stop_price is not None and mark_price >= stop_price:
            return "stop_loss"
        if take_profit_price is not None and mark_price <= take_profit_price:
            return "take_profit"
    if time_stop_ts is not None and now >= time_stop_ts:
        return "time_stop"
    return None
