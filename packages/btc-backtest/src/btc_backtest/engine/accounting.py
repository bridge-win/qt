"""Deterministic decimal portfolio accounting for spot and perpetual BTC."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from btc_backtest.engine.models import (
    Fill,
    FundingEvent,
    InstrumentKind,
    OrderSide,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.errors import ExecutionError


@dataclass
class _PositionState:
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    funding_pnl: Decimal = Decimal("0")


class Portfolio:
    """Apply each fill/funding event once and reconcile marked equity."""

    def __init__(self, initial_cash: Decimal) -> None:
        if not initial_cash.is_finite() or initial_cash <= 0:
            raise ValueError("initial_cash must be finite and positive")
        self._cash = initial_cash
        self._positions = {
            InstrumentKind.SPOT: _PositionState(),
            InstrumentKind.PERPETUAL: _PositionState(),
        }
        self._fill_ids: set[str] = set()
        self._funding_ids: set[str] = set()
        self._last_timestamp: datetime | None = None
        self._marks: dict[InstrumentKind, Decimal] = {}

    def apply_fill(self, fill: Fill) -> PortfolioSnapshot:
        if fill.id in self._fill_ids:
            raise ExecutionError(f"duplicate fill id: {fill.id}")
        self._require_chronological(fill.timestamp)
        if fill.instrument is InstrumentKind.SPOT:
            self._apply_spot_fill(fill)
        else:
            self._apply_perpetual_fill(fill)
        self._fill_ids.add(fill.id)
        self._marks[fill.instrument] = fill.price
        self._last_timestamp = fill.timestamp
        return self._snapshot(fill.timestamp)

    def apply_funding(self, event: FundingEvent) -> PortfolioSnapshot:
        if event.id in self._funding_ids:
            raise ExecutionError(f"duplicate funding id: {event.id}")
        self._require_chronological(event.timestamp)
        state = self._positions[InstrumentKind.PERPETUAL]
        if state.quantity == 0:
            raise ExecutionError("cannot apply perpetual funding without a position")
        next_cash = self._cash + event.amount
        if next_cash < 0:
            raise ExecutionError("funding payment exceeds available cash")
        self._cash = next_cash
        state.realized_pnl += event.amount
        state.funding_pnl += event.amount
        self._funding_ids.add(event.id)
        self._last_timestamp = event.timestamp
        return self._snapshot(event.timestamp)

    def mark(
        self,
        timestamp: datetime,
        marks: Mapping[str | InstrumentKind, Decimal] | Decimal,
    ) -> PortfolioSnapshot:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("mark timestamp must be timezone-aware")
        self._require_chronological(timestamp)
        if isinstance(marks, Decimal):
            self._set_mark(InstrumentKind.SPOT, marks)
        else:
            for instrument, value in marks.items():
                self._set_mark(InstrumentKind(instrument), value)
        self._last_timestamp = timestamp
        return self._snapshot(timestamp)

    def _apply_spot_fill(self, fill: Fill) -> None:
        state = self._positions[InstrumentKind.SPOT]
        if fill.side is OrderSide.BUY:
            debit = fill.notional + fill.fee
            if debit > self._cash:
                raise ExecutionError("spot buy exceeds available cash")
            prior_cost = state.average_price * state.quantity
            next_quantity = state.quantity + fill.quantity
            state.average_price = (prior_cost + fill.notional) / next_quantity
            state.quantity = next_quantity
            self._cash -= debit
            state.realized_pnl -= fill.fee
            state.fees_paid += fill.fee
            return

        if fill.quantity > state.quantity:
            raise ExecutionError("spot sell exceeds current holdings")
        realized = (fill.price - state.average_price) * fill.quantity - fill.fee
        state.quantity -= fill.quantity
        self._cash += fill.notional - fill.fee
        state.realized_pnl += realized
        state.fees_paid += fill.fee
        if state.quantity == 0:
            state.average_price = Decimal("0")

    def _apply_perpetual_fill(self, fill: Fill) -> None:
        state = self._positions[InstrumentKind.PERPETUAL]
        if fill.side is OrderSide.SELL:
            if fill.fee > self._cash:
                raise ExecutionError("perpetual fee exceeds available cash")
            prior_quantity = abs(state.quantity)
            next_quantity = prior_quantity + fill.quantity
            state.average_price = (
                state.average_price * prior_quantity + fill.notional
            ) / next_quantity
            state.quantity = -next_quantity
            self._cash -= fill.fee
            state.realized_pnl -= fill.fee
            state.fees_paid += fill.fee
            return

        open_short = abs(state.quantity)
        if open_short == 0:
            raise ExecutionError("directional long perpetual positions are unsupported")
        if fill.quantity > open_short:
            raise ExecutionError(
                "perpetual buy would create an unsupported long perpetual position"
            )
        gross = (state.average_price - fill.price) * fill.quantity
        next_cash = self._cash + gross - fill.fee
        if next_cash < 0:
            raise ExecutionError("perpetual close exceeds available cash")
        state.quantity += fill.quantity
        self._cash = next_cash
        state.realized_pnl += gross - fill.fee
        state.fees_paid += fill.fee
        if state.quantity == 0:
            state.average_price = Decimal("0")

    def _snapshot(self, timestamp: datetime) -> PortfolioSnapshot:
        spot = self._positions[InstrumentKind.SPOT]
        perpetual = self._positions[InstrumentKind.PERPETUAL]
        spot_mark = self._marks.get(InstrumentKind.SPOT, spot.average_price)
        perpetual_mark = self._marks.get(
            InstrumentKind.PERPETUAL,
            perpetual.average_price,
        )
        spot_unrealized = (spot_mark - spot.average_price) * spot.quantity
        perpetual_unrealized = (
            perpetual_mark - perpetual.average_price
        ) * perpetual.quantity
        unrealized = spot_unrealized + perpetual_unrealized
        equity = (
            self._cash
            + spot.quantity * spot_mark
            + perpetual_unrealized
        )
        positions = (
            self._position_model(InstrumentKind.SPOT, spot),
            self._position_model(InstrumentKind.PERPETUAL, perpetual),
        )
        return PortfolioSnapshot(
            timestamp=timestamp,
            cash=self._cash,
            equity=equity,
            realized_pnl=spot.realized_pnl + perpetual.realized_pnl,
            unrealized_pnl=unrealized,
            positions=positions,
        )

    @staticmethod
    def _position_model(
        instrument: InstrumentKind,
        state: _PositionState,
    ) -> Position:
        return Position(
            instrument=instrument,
            quantity=state.quantity,
            average_price=state.average_price,
            realized_pnl=state.realized_pnl,
            fees_paid=state.fees_paid,
            funding_pnl=state.funding_pnl,
        )

    def _set_mark(self, instrument: InstrumentKind, value: Decimal) -> None:
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{instrument.value} mark must be finite and positive")
        self._marks[instrument] = value

    def _require_chronological(self, timestamp: datetime) -> None:
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ExecutionError("portfolio event timestamp moved backwards")
