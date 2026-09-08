"""Causal bridge from the btc-backtest strategy protocol to Nautilus orders."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from math import isfinite
from typing import Protocol, cast

import pandas as pd
from btc_backtest.engine.models import (
    InstrumentKind,
    OrderIntent,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.engine.models import (
    OrderSide as LegacyOrderSide,
)
from btc_backtest.strategies.base import Strategy

from qt.nautilus.models import DecisionTrace
from qt.strategy_ports.btcqt import (
    BtcqtNativeStrategy,
    BtcqtStrategyPort,
    CausalState,
    PortDecision,
)
from qt.strategy_ports.freqtrade import (
    FreqtradeDecision,
    FreqtradeNativeStrategy,
    FreqtradeOrder,
    FreqtradeProtectionState,
    FreqtradeResearchPort,
    FreqtradeTradeState,
)


class NativeMoney(Protocol):
    def as_decimal(self) -> Decimal: ...


class NativeAccount(Protocol):
    def balance_free(self, currency: object) -> NativeMoney | None: ...

    def balance_total(self, currency: object) -> NativeMoney | None: ...


class NativePortfolio(Protocol):
    def account(self, *, venue: object) -> NativeAccount | None: ...

    def equity(self, *, venue: object) -> Mapping[object, NativeMoney]: ...

    def net_position(self, instrument_id: object) -> Decimal: ...

    def realized_pnls(
        self,
        *,
        venue: object,
        target_currency: object,
    ) -> Mapping[object, NativeMoney]: ...

    def unrealized_pnls(
        self,
        *,
        venue: object,
        target_currency: object,
    ) -> Mapping[object, NativeMoney]: ...


class NativeInstrument(Protocol):
    id: object
    base_currency: object
    quote_currency: object
    price_increment: NativeMoney

    def make_price(self, value: float) -> object: ...

    def make_qty(self, value: float) -> object: ...


class NativeBar(Protocol):
    ts_event: int
    ts_init: int
    close: object


@dataclass(frozen=True)
class NativeFillTransaction:
    """One native fill event before reports aggregate it by order."""

    side: str
    quantity: Decimal
    price: Decimal
    commission: str | None


@dataclass
class _BtcqtSubmittedOrder:
    kind: str
    tag: str
    side: str
    state: CausalState
    client_order_id: object
    instrument: NativeInstrument
    fill_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _BtcqtPendingProtection:
    kind: str
    state: CausalState
    reason: str
    side: str
    price: float
    quantity: float


class BtcqtNativeIntentRouter:
    """Submit unmodified btc-qt intents and return actual native fills to the port.

    This deliberately owns the lifecycle commands which cannot be represented
    by ``btc_backtest.OrderIntent``: IOC, ladder replacement, cancellation,
    take-profit and stop protection.
    """

    def __init__(
        self,
        port: BtcqtStrategyPort,
        *,
        perpetual_instrument: NativeInstrument,
    ) -> None:
        self.port = port
        self._perpetual_instrument = perpetual_instrument
        self._submitted: dict[str, _BtcqtSubmittedOrder] = {}
        self._entry_ids: set[str] = set()
        self._causal_states: dict[datetime, CausalState] = {}
        self._active_protection: dict[str, str] = {}
        self._pending_protection: dict[str, _BtcqtPendingProtection] = {}

    def submit_decision(self, strategy: object, decision: PortDecision) -> None:
        """Interpret every source action with its original order semantics."""
        try:
            from nautilus_trader.model import TimeInForce
        except ImportError as error:  # pragma: no cover - native host capability
            raise RuntimeError("NautilusTrader 2.0.0rc4 is required for source intents") from error

        self._remember_causal_state(decision.state)
        for intent in decision.intents:
            action = intent.action
            if action == "CANCEL_ALL":
                self._cancel_all(strategy)
                continue
            if action in {"CANCEL_LADDER", "CANCEL_ENTRIES"}:
                self._cancel_entries(
                    strategy, intent.side.value if intent.side is not None else None
                )
                continue
            if action == "REPLACE_LADDER":
                side = _btcqt_required_side(intent.action, intent.side)
                self._cancel_entries(strategy, side)
                for index, (price, quantity) in enumerate(
                    zip(intent.prices, intent.qtys, strict=True)
                ):
                    if quantity > 0:
                        self._submit_limit(
                            strategy,
                            decision.state,
                            action,
                            intent.reason,
                            side,
                            price,
                            quantity,
                            post_only=True,
                            entry=True,
                            tag=f"ladder:{index}",
                        )
                continue
            side = _btcqt_required_side(action, intent.side)
            if action == "MARKET_ENTER":
                self._submit_market(
                    strategy,
                    decision.state,
                    intent.reason,
                    side,
                    _btcqt_positive(intent.qty, action),
                    time_in_force=TimeInForce.GTC,
                    reduce_only=False,
                    entry=True,
                )
            elif action == "LIMIT_IOC_ENTER":
                self._submit_limit(
                    strategy,
                    decision.state,
                    action,
                    intent.reason,
                    side,
                    _btcqt_positive(intent.price, action),
                    _btcqt_positive(intent.qty, action),
                    post_only=False,
                    entry=True,
                    tag="ioc",
                    time_in_force=TimeInForce.IOC,
                )
            elif action == "MARKET_EXIT":
                self._submit_market(
                    strategy,
                    decision.state,
                    intent.reason,
                    side,
                    _btcqt_positive(intent.qty, action),
                    time_in_force=TimeInForce.GTC,
                    reduce_only=True,
                    entry=False,
                )
            elif action == "PLACE_TP":
                self._request_protection(
                    strategy,
                    _BtcqtPendingProtection(
                        "TP",
                        decision.state,
                        intent.reason,
                        side,
                        _btcqt_positive(intent.price, action),
                        _btcqt_positive(intent.qty, action),
                    ),
                )
            elif action == "PLACE_STOP":
                self._request_protection(
                    strategy,
                    _BtcqtPendingProtection(
                        "STOP",
                        decision.state,
                        intent.reason,
                        side,
                        _btcqt_positive(intent.price, action),
                        _btcqt_positive(intent.qty, action),
                    ),
                )
            else:
                raise ValueError(f"unsupported btc-qt source intent: {action}")

    def on_order_filled(self, strategy: object, event: object) -> None:
        """Convert the actual Nautilus fill and submit source-generated follow-ups."""
        client_order_id = str(event.client_order_id)
        submitted = self._submitted.get(client_order_id)
        if submitted is None:
            return
        from nautilus_trader.model import OrderSide

        from qt.legacy.btcqt.models import Fill, Side

        fill_id = str(event.trade_id)
        if fill_id in submitted.fill_ids:
            return
        submitted.fill_ids.add(fill_id)
        for kind, active_id in tuple(self._active_protection.items()):
            if active_id == client_order_id:
                self._active_protection.pop(kind, None)

        fill = Fill(
            ts=int(event.ts_event) // 1_000_000,
            order_id=client_order_id,
            strategy=self.port.strategy.name,
            side=Side.BUY if event.order_side is OrderSide.BUY else Side.SELL,
            price=float(event.last_px),
            qty=float(event.last_qty),
            fee=_native_fee(
                event.commission,
                quote_currency=submitted.instrument.quote_currency,
                base_currency=submitted.instrument.base_currency,
                fill_price=Decimal(str(event.last_px)),
            ),
            kind=submitted.kind,
            tag=submitted.tag,
        )
        state = self._state_at(fill.ts)
        self.submit_decision(strategy, self.port.on_fill(fill, state))
        self._discard_if_closed(strategy, client_order_id)

    def on_order_canceled(self, strategy: object, event: object) -> None:
        client_order_id = str(event.client_order_id)
        self._submitted.pop(client_order_id, None)
        self._entry_ids.discard(client_order_id)
        for kind, active_id in tuple(self._active_protection.items()):
            if active_id != client_order_id:
                continue
            self._active_protection.pop(kind, None)
            pending = self._pending_protection.pop(kind, None)
            if pending is not None:
                self._submit_protection(strategy, pending)

    def on_order_rejected(self, strategy: object, event: object) -> None:
        self.on_order_canceled(strategy, event)

    def _entry_instrument(self, side: str) -> NativeInstrument:
        del side
        # btcqt.execution.gateway always uses Binance USDT-M BTC/USDT:USDT.
        # Direction is encoded solely by order side; a BUY is never a spot leg.
        return self._perpetual_instrument

    def _position_instrument(self, action: str) -> NativeInstrument:
        del action
        return self._perpetual_instrument

    def _submit_market(
        self,
        strategy: object,
        state: CausalState,
        reason: str,
        side: str,
        quantity: float,
        *,
        time_in_force: object,
        reduce_only: bool,
        entry: bool,
    ) -> None:
        from nautilus_trader.model import OrderSide

        instrument = (
            self._entry_instrument(side)
            if entry
            else self._position_instrument("MARKET_EXIT")
        )
        order = strategy.order_factory.market(
            instrument_id=instrument.id,
            order_side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            quantity=instrument.make_qty(quantity),
            time_in_force=time_in_force,
            reduce_only=reduce_only,
            tags=[self.port.strategy.name, reason, "market"],
        )
        self._submit(strategy, order, "MARKET", "market", state, instrument, entry=entry)

    def _submit_limit(
        self,
        strategy: object,
        state: CausalState,
        action: str,
        reason: str,
        side: str,
        price: float,
        quantity: float,
        *,
        post_only: bool,
        entry: bool,
        tag: str,
        reduce_only: bool = False,
        time_in_force: object | None = None,
    ) -> None:
        from nautilus_trader.model import OrderSide

        instrument = self._entry_instrument(side) if entry else self._position_instrument(action)
        order = strategy.order_factory.limit(
            instrument_id=instrument.id,
            order_side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            quantity=instrument.make_qty(quantity),
            price=instrument.make_price(price),
            time_in_force=time_in_force,
            post_only=post_only,
            reduce_only=reduce_only,
            tags=[self.port.strategy.name, reason, action, tag],
        )
        self._submit(
            strategy,
            order,
            "TP" if tag == "tp" else "LIMIT",
            tag,
            state,
            instrument,
            entry=entry,
        )

    def _request_protection(self, strategy: object, pending: _BtcqtPendingProtection) -> None:
        """Mirror gateway.py: replace one source stop or TP, never stack it."""

        active_id = self._active_protection.get(pending.kind)
        if active_id is None:
            self._submit_protection(strategy, pending)
            return
        self._pending_protection[pending.kind] = pending
        active = self._submitted.get(active_id)
        if active is None:
            self._active_protection.pop(pending.kind, None)
            replacement = self._pending_protection.pop(pending.kind)
            self._submit_protection(strategy, replacement)
            return
        strategy.cancel_order(active.client_order_id)

    def _submit_protection(self, strategy: object, pending: _BtcqtPendingProtection) -> None:
        from nautilus_trader.model import OrderSide

        instrument = self._perpetual_instrument
        if pending.kind == "TP":
            order = strategy.order_factory.limit(
                instrument_id=instrument.id,
                order_side=OrderSide.BUY if pending.side == "BUY" else OrderSide.SELL,
                quantity=instrument.make_qty(pending.quantity),
                price=instrument.make_price(pending.price),
                post_only=False,
                reduce_only=True,
                tags=[self.port.strategy.name, pending.reason, "PLACE_TP", "tp"],
            )
            tag = "tp"
        elif pending.kind == "STOP":
            order = strategy.order_factory.stop_market(
                instrument_id=instrument.id,
                order_side=OrderSide.BUY if pending.side == "BUY" else OrderSide.SELL,
                quantity=instrument.make_qty(pending.quantity),
                trigger_price=instrument.make_price(pending.price),
                reduce_only=True,
                tags=[self.port.strategy.name, pending.reason, "stop"],
            )
            tag = "stop"
        else:  # pragma: no cover - local closed set
            raise ValueError(f"unsupported btc-qt protection kind: {pending.kind}")
        self._submit(strategy, order, pending.kind, tag, pending.state, instrument, entry=False)
        self._active_protection[pending.kind] = str(order.client_order_id)

    def _submit(
        self,
        strategy: object,
        order: object,
        kind: str,
        tag: str,
        state: CausalState,
        instrument: NativeInstrument,
        *,
        entry: bool,
    ) -> None:
        client_order_id = str(order.client_order_id)
        self._submitted[client_order_id] = _BtcqtSubmittedOrder(
            kind,
            tag,
            _native_order_side(order),
            state.snapshot(),
            order.client_order_id,
            instrument,
        )
        if entry:
            self._entry_ids.add(client_order_id)
        strategy.submit_order(order)

    def _cancel_entries(self, strategy: object, side: str | None) -> None:
        for client_order_id in tuple(self._entry_ids):
            submitted = self._submitted.get(client_order_id)
            if submitted is None:
                self._entry_ids.discard(client_order_id)
                continue
            if side is None or self._entry_side(client_order_id) == side:
                strategy.cancel_order(submitted.client_order_id)

    def _entry_side(self, client_order_id: str) -> str:
        # Source entry orders are keyed by their native instrument identity.
        # This avoids cancelling an opposite-side ladder when the source asks
        # for a one-sided cancellation.
        return self._submitted[client_order_id].side

    def _cancel_all(self, strategy: object) -> None:
        strategy.cancel_all_orders(self._perpetual_instrument.id, strategy_only=True)
        self._submitted.clear()
        self._entry_ids.clear()
        self._active_protection.clear()
        self._pending_protection.clear()

    def _remember_causal_state(self, state: CausalState) -> None:
        """Store an immutable close snapshot for a later delayed fill callback."""

        prior = self._causal_states.get(state.observed_at)
        snapshot = state.snapshot()
        if prior is not None:
            if prior != snapshot:
                raise ValueError(
                    "btc-qt native router received conflicting causal states "
                    f"for {state.observed_at.isoformat()}"
                )
            return
        self._causal_states[state.observed_at] = snapshot

    def _state_at(self, fill_timestamp_ms: int) -> CausalState:
        fill_at = datetime.fromtimestamp(fill_timestamp_ms / 1000, tz=timezone.utc)
        visible = [
            state
            for state in self._causal_states.values()
            if state.observed_at <= fill_at and state.available_at <= fill_at
        ]
        if not visible:
            raise ValueError(
                "btc-qt fill has no causal state available at "
                f"{fill_at.isoformat()}"
            )
        return max(visible, key=lambda state: state.observed_at).snapshot()

    def _discard_if_closed(self, strategy: object, client_order_id: str) -> None:
        cache = getattr(strategy, "cache", None)
        lookup = getattr(cache, "order", None)
        if not callable(lookup):
            return
        order = lookup(self._submitted[client_order_id].client_order_id)
        if order is not None and order.is_closed():
            self._submitted.pop(client_order_id, None)
            self._entry_ids.discard(client_order_id)


@dataclass
class _FreqtradeSubmittedOrder:
    source_order: FreqtradeOrder
    client_order_id: object
    fill_ids: set[str] = field(default_factory=set)


class FreqtradeNativeIntentRouter:
    """Preserve Freqtrade source order, stop, fill, and protection lifecycles.

    This is intentionally separate from ``order_intents``: the source port's
    ``replace_stop`` action must remain a native cancel-and-replace operation,
    and every partial native fill must be applied to its source trade state.
    """

    def __init__(self, port: FreqtradeResearchPort, *, instrument: NativeInstrument) -> None:
        self.port = port
        self.instrument = instrument
        self.trade: FreqtradeTradeState | None = None
        self.protection_state = FreqtradeProtectionState()
        self._submitted: dict[str, _FreqtradeSubmittedOrder] = {}
        self._active_stop_id: str | None = None
        self._pending_stop: FreqtradeOrder | None = None

    def submit_decision(self, strategy: object, decision: FreqtradeDecision) -> None:
        """Submit source orders, preserving stop replacement as a lifecycle."""
        self.protection_state = decision.protection_state
        for order in decision.orders:
            if order.action == "enter_long":
                self._submit_market(strategy, order, reduce_only=False)
            elif order.action == "exit_long":
                self._submit_market(strategy, order, reduce_only=True)
            elif order.action == "replace_stop":
                self._request_stop(strategy, order)
            else:  # pragma: no cover - literal action contract guards this
                raise ValueError(f"unsupported Freqtrade source action: {order.action}")

    def on_order_filled(
        self,
        strategy: object,
        event: object,
        *,
        equity_after: Decimal,
    ) -> None:
        """Apply an actual native partial fill and dispatch source follow-ups."""
        client_order_id = str(event.client_order_id)
        submitted = self._submitted.get(client_order_id)
        if submitted is None:
            return
        fill_id = str(event.trade_id)
        if fill_id in submitted.fill_ids:
            return
        submitted.fill_ids.add(fill_id)
        filled_at = datetime.fromtimestamp(int(event.ts_event) / 1_000_000_000, tz=timezone.utc)
        source_order = submitted.source_order
        self.trade = self.port.on_fill(
            source_order,
            order_id=source_order.order_id,
            fill_price=float(event.last_px),
            fill_quantity=float(event.last_qty),
            filled_at=filled_at,
            fees=_native_fee(
                event.commission,
                quote_currency=self.instrument.quote_currency,
                base_currency=self.instrument.base_currency,
                fill_price=Decimal(str(event.last_px)),
            ),
            trade=self.trade,
        )
        if source_order.action == "enter_long" and self.trade is not None and not self.trade.is_closed:
            self._request_stop(
                strategy,
                self.port.initial_stop_order(self.trade, submitted_at=filled_at),
            )
        elif source_order.action == "exit_long" and self.trade is not None and self.trade.is_closed:
            self.protection_state = self.port.on_trade_closed(
                self.protection_state,
                closed_at=filled_at,
                equity_after=float(equity_after),
            )
        self._discard_if_closed(strategy, client_order_id)

    def on_order_accepted(self, event: object) -> None:
        client_order_id = str(event.client_order_id)
        if client_order_id != self._active_stop_id or self.trade is None:
            return
        submitted = self._submitted.get(client_order_id)
        if submitted is None:
            return
        self.trade = self.port.on_stop_accepted(self.trade, submitted.source_order)

    def on_order_canceled(self, strategy: object, event: object) -> None:
        client_order_id = str(event.client_order_id)
        self._submitted.pop(client_order_id, None)
        if client_order_id != self._active_stop_id:
            return
        self._active_stop_id = None
        pending = self._pending_stop
        self._pending_stop = None
        if pending is not None:
            self._submit_stop(strategy, pending)

    def on_order_rejected(self, strategy: object, event: object) -> None:
        self.on_order_canceled(strategy, event)

    def _submit_market(self, strategy: object, order: FreqtradeOrder, *, reduce_only: bool) -> None:
        from nautilus_trader.model import OrderSide

        native = strategy.order_factory.market(
            instrument_id=self.instrument.id,
            order_side=OrderSide.SELL if reduce_only else OrderSide.BUY,
            quantity=self.instrument.make_qty(order.quantity),
            reduce_only=reduce_only,
            tags=[self.port.metadata.strategy_id, order.reason, order.action],
        )
        self._remember_and_submit(strategy, native, order)

    def _request_stop(self, strategy: object, order: FreqtradeOrder) -> None:
        if order.price is None:
            raise ValueError("Freqtrade replace_stop requires a source price")
        if self._active_stop_id is None:
            self._submit_stop(strategy, order)
            return
        self._pending_stop = order
        active = self._submitted.get(self._active_stop_id)
        if active is None:
            self._active_stop_id = None
            pending = self._pending_stop
            self._pending_stop = None
            if pending is not None:
                self._submit_stop(strategy, pending)
            return
        strategy.cancel_order(active.client_order_id)

    def _submit_stop(self, strategy: object, order: FreqtradeOrder) -> None:
        from nautilus_trader.model import OrderSide

        if order.price is None:
            raise ValueError("Freqtrade replace_stop requires a source price")
        native = strategy.order_factory.stop_market(
            instrument_id=self.instrument.id,
            order_side=OrderSide.SELL,
            quantity=self.instrument.make_qty(order.quantity),
            trigger_price=self.instrument.make_price(order.price),
            reduce_only=True,
            tags=[self.port.metadata.strategy_id, order.reason, "stop"],
        )
        self._remember_and_submit(strategy, native, order)
        self._active_stop_id = str(native.client_order_id)

    def _remember_and_submit(self, strategy: object, native: object, order: FreqtradeOrder) -> None:
        self._submitted[str(native.client_order_id)] = _FreqtradeSubmittedOrder(
            source_order=order,
            client_order_id=native.client_order_id,
        )
        strategy.submit_order(native)

    def _discard_if_closed(self, strategy: object, client_order_id: str) -> None:
        cache = getattr(strategy, "cache", None)
        lookup = getattr(cache, "order", None)
        if not callable(lookup):
            return
        submitted = self._submitted.get(client_order_id)
        if submitted is None:
            return
        order = lookup(submitted.client_order_id)
        if order is not None and order.is_closed():
            self._submitted.pop(client_order_id, None)

def _btcqt_required_side(action: str, side: object) -> str:
    raw = getattr(side, "value", side)
    if raw not in {"BUY", "SELL"}:
        raise ValueError(f"{action} requires a BUY or SELL side")
    return str(raw)


def _btcqt_positive(value: float | None, action: str) -> float:
    if value is None or value <= 0:
        raise ValueError(f"{action} requires a positive price or quantity")
    return float(value)


def _native_fee(
    commission: object,
    *,
    quote_currency: object,
    base_currency: object,
    fill_price: Decimal,
) -> float:
    """Return the quote fee expected by the legacy btcqt Fill model.

    Native fills can charge in base or quote currency. A source ``Fill.fee``
    has no currency field, so passing a base amount through would mix BTC and
    quote units in source accounting. Convert only against this actual fill;
    a third currency requires explicit contemporaneous FX and is rejected.
    """
    if commission is None:
        return 0.0
    try:
        amount_text, currency = str(commission).split(maxsplit=1)
        amount = Decimal(amount_text)
    except (ArithmeticError, ValueError) as error:
        raise ValueError("native commission must be a numeric amount and currency") from error
    if amount < 0 or fill_price <= 0:
        raise ValueError("native commission and fill price must be positive")
    if currency == str(quote_currency):
        return float(amount)
    if currency == str(base_currency):
        return float(amount * fill_price)
    raise ValueError(
        "native commission currency requires explicit fill-time FX conversion: "
        f"{currency}, expected {quote_currency} or {base_currency}",
    )


def _native_order_side(order: object) -> str:
    from nautilus_trader.model import OrderSide

    side = getattr(order, "side", None)
    if side is OrderSide.BUY:
        return "BUY"
    if side is OrderSide.SELL:
        return "SELL"
    raise ValueError("native order has no BUY/SELL identity")


class CausalStrategyBridge:
    """Call an existing strategy with a history ending at the active bar only."""

    def __init__(self, strategy: Strategy, parameters: Mapping[str, object]) -> None:
        self.strategy = strategy
        self.parameters = dict(parameters)
        self._traces: list[DecisionTrace] = []

    @property
    def traces(self) -> tuple[DecisionTrace, ...]:
        return tuple(self._traces)

    def decide(
        self,
        *,
        timestamp: datetime,
        bars: pd.DataFrame,
        history_end: pd.Timestamp | None = None,
        cash: Decimal,
        equity: Decimal,
        positions: Mapping[InstrumentKind, Decimal],
        realized_pnl: Decimal = Decimal("0"),
        unrealized_pnl: Decimal = Decimal("0"),
    ) -> tuple[OrderIntent, ...]:
        """Create intents without exposing rows after ``timestamp`` to the strategy."""
        from btc_backtest.strategies.base import StrategyContext

        cutoff = history_end if history_end is not None else pd.Timestamp(timestamp)
        # Avoid a boolean-mask materialization here. ``StrategyContext``
        # performs its own defensive isolation required by the existing
        # strategy protocol, so this only removes the redundant bridge copy.
        visible = bars.iloc[: bars.index.searchsorted(cutoff, side="right")]
        if visible.empty:
            raise ValueError("strategy cannot decide without an active bar")
        if visible.index[-1] > pd.Timestamp(timestamp):
            raise ValueError("strategy context would contain future data")
        snapshot = PortfolioSnapshot(
            timestamp=timestamp,
            cash=cash,
            equity=equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            positions=tuple(
                Position(instrument=kind, quantity=quantity)
                for kind, quantity in sorted(positions.items(), key=lambda item: item[0].value)
            ),
        )
        context = StrategyContext(
            timestamp=timestamp,
            bars=visible,
            portfolio=snapshot,
            parameters=self.parameters,
        )
        intents = tuple(self.strategy.on_bar(context))
        explanation = (
            ("native_cash", str(cash)),
            ("native_equity", str(equity)),
            ("native_realized_pnl", str(realized_pnl)),
            ("native_unrealized_pnl", str(unrealized_pnl)),
            *_explanation(self.strategy, context),
        )
        self._traces.append(
            DecisionTrace(
                timestamp=timestamp,
                strategy_id=self.strategy.metadata.id,
                observed_rows=len(visible),
                intent_count=len(intents),
                reasons=tuple(intent.reason for intent in intents),
                no_trade_cause=None if intents else _no_trade_cause(visible, self.strategy),
                observed_values=explanation,
            )
        )
        return intents

    def record_freqtrade_decision(
        self,
        *,
        timestamp: datetime,
        bars: pd.DataFrame,
        history_end: pd.Timestamp,
        decision: FreqtradeDecision,
    ) -> None:
        """Persist a causal source decision without flattening it to intents."""

        visible = bars.iloc[: bars.index.searchsorted(history_end, side="right")]
        if visible.empty or visible.index[-1] > history_end:
            raise ValueError("Freqtrade source decision has no completed causal bar")
        orders = decision.orders
        self._traces.append(
            DecisionTrace(
                timestamp=timestamp,
                strategy_id=self.strategy.metadata.id,
                observed_rows=len(visible),
                intent_count=len(orders),
                reasons=tuple(order.reason for order in orders),
                no_trade_cause=None if orders else "freqtrade_source_no_order",
                observed_values=(
                    ("source_strategy_id", decision.strategy_id),
                    ("source_profile_fingerprint", decision.profile_fingerprint),
                    ("source_orders", tuple(order.action for order in orders)),
                ),
            )
        )

    def record_btcqt_decision(
        self,
        *,
        bars: pd.DataFrame,
        history_end: pd.Timestamp,
        decision: PortDecision,
    ) -> None:
        """Persist the source state, evidence identities, and unflattened intents."""

        visible = bars.iloc[: bars.index.searchsorted(history_end, side="right")]
        if visible.empty or visible.index[-1] > history_end:
            raise ValueError("btc-qt source decision has no completed causal bar")
        self._traces.append(
            DecisionTrace(
                timestamp=decision.timestamp,
                strategy_id=self.strategy.metadata.id,
                observed_rows=len(visible),
                intent_count=len(decision.intents),
                reasons=tuple(intent.reason for intent in decision.intents),
                no_trade_cause=None if decision.intents else "btcqt_source_no_intent",
                observed_values=(
                    ("source_strategy_id", decision.strategy_id),
                    ("source_actions", tuple(intent.action for intent in decision.intents)),
                    (
                        "causal_inputs",
                        tuple(
                            (item.dataset_id, item.version, item.available_at.isoformat())
                            for item in decision.state.inputs
                        ),
                    ),
                ),
            )
        )


class NativeStrategyFactory:
    """Build a real Nautilus ``Strategy`` subclass after its extension is loaded."""

    def __init__(
        self,
        bridge: CausalStrategyBridge,
        *,
        frame: pd.DataFrame,
        instrument_ids: Mapping[InstrumentKind, object],
        bar_types: Mapping[InstrumentKind, object],
        quote_currency: object,
        venue: object,
        initial_cash: Decimal,
        decision_start: pd.Timestamp | None = None,
        cancelled: Callable[[], bool] | None = None,
        taker_fee: Decimal = Decimal("0"),
        subscribe_quotes: bool = False,
    ) -> None:
        self.bridge = bridge
        self.frame = frame
        self.instrument_ids = dict(instrument_ids)
        self.bar_types = dict(bar_types)
        self.quote_currency = quote_currency
        self.venue = venue
        self.initial_cash = initial_cash
        self.decision_start = decision_start
        self.cancelled = cancelled
        self.was_cancelled = False
        self.taker_fee = taker_fee
        self.subscribe_quotes = subscribe_quotes
        self.native_fill_transactions: list[NativeFillTransaction] = []

    def create(self) -> object:
        """Return a native strategy which preserves each legacy order intent."""
        try:
            from nautilus_trader.model import OrderSide
            from nautilus_trader.trading import Strategy as NautilusStrategy
        except ImportError as error:  # pragma: no cover - host capability
            raise RuntimeError("NautilusTrader 2.0.0rc4 is required for execution") from error

        factory = self
        source_strategy = factory.bridge.strategy
        freqtrade_router = (
            FreqtradeNativeIntentRouter(
                source_strategy.port,
                instrument=cast(
                    NativeInstrument,
                    factory.instrument_ids[InstrumentKind.SPOT],
                ),
            )
            if isinstance(source_strategy, FreqtradeNativeStrategy)
            else None
        )
        btcqt_router = (
            BtcqtNativeIntentRouter(
                source_strategy.port,
                perpetual_instrument=cast(
                    NativeInstrument,
                    factory.instrument_ids[InstrumentKind.PERPETUAL],
                ),
            )
            if isinstance(source_strategy, BtcqtNativeStrategy)
            else None
        )

        class AdaptedStrategy(NautilusStrategy):
            def on_start(self) -> None:
                for bar_type in factory.bar_types.values():
                    self.subscribe_bars(bar_type)
                if factory.subscribe_quotes:
                    for instrument in factory.instrument_ids.values():
                        self.subscribe_quotes(instrument.id)

            def on_quote(self, quote: object) -> None:
                """Keep the native cache/accounting path subscribed to attested marks."""
                del quote

            def on_bar(self, bar: object) -> None:
                if factory.cancelled is not None and factory.cancelled():
                    factory.was_cancelled = True
                    self.stop()
                    return
                native_bar = cast(NativeBar, bar)
                ts_init = native_bar.ts_init
                timestamp = pd.Timestamp(ts_init, unit="ns", tz="UTC").to_pydatetime()
                history_end = pd.Timestamp(native_bar.ts_event, unit="ns", tz="UTC")
                if factory.decision_start is not None and history_end < factory.decision_start:
                    return
                cash, equity, realized_pnl, unrealized_pnl, positions = _native_portfolio_state(
                    self.portfolio,
                    factory.instrument_ids,
                    factory.quote_currency,
                    factory.venue,
                    factory.initial_cash,
                    timestamp,
                    Decimal(str(native_bar.close)),
                )
                if freqtrade_router is not None:
                    decision_time = history_end.to_pydatetime()
                    decision = source_strategy.native_decision(
                        timestamp=decision_time,
                        bars=factory.frame,
                        equity=equity,
                        trade=freqtrade_router.trade,
                        protection_state=freqtrade_router.protection_state,
                    )
                    factory.bridge.record_freqtrade_decision(
                        timestamp=decision_time,
                        bars=factory.frame,
                        history_end=history_end,
                        decision=decision,
                    )
                    freqtrade_router.submit_decision(self, decision)
                    return
                if btcqt_router is not None:
                    decision = source_strategy.native_decision(
                        timestamp=history_end.to_pydatetime(),
                        bars=factory.frame,
                        equity=equity,
                    )
                    factory.bridge.record_btcqt_decision(
                        bars=factory.frame,
                        history_end=history_end,
                        decision=decision,
                    )
                    btcqt_router.submit_decision(self, decision)
                    return
                for intent in factory.bridge.decide(
                    timestamp=timestamp,
                    bars=factory.frame,
                    history_end=history_end,
                    cash=cash,
                    equity=equity,
                    realized_pnl=realized_pnl,
                    unrealized_pnl=unrealized_pnl,
                    positions=positions,
                ):
                    raw_instrument = factory.instrument_ids.get(intent.instrument)
                    if raw_instrument is None:
                        raise ValueError(f"missing native instrument for {intent.instrument.value}")
                    instrument = cast(NativeInstrument, raw_instrument)
                    quantity = _intent_quantity(
                        intent,
                        native_bar,
                        instrument,
                        taker_fee=factory.taker_fee,
                    )
                    side = OrderSide.BUY if intent.side is LegacyOrderSide.BUY else OrderSide.SELL
                    tags = [intent.reason, *(intent.signal_ids or ())]
                    if intent.order_type.value == "market":
                        order = self.order_factory.market(
                            instrument_id=instrument.id,
                            order_side=side,
                            quantity=quantity,
                            reduce_only=_reduce_only(intent),
                            tags=tags,
                        )
                    elif intent.order_type.value == "limit":
                        order = self.order_factory.limit(
                            instrument_id=instrument.id,
                            order_side=side,
                            quantity=quantity,
                            price=instrument.make_price(float(intent.limit_price)),
                            reduce_only=_reduce_only(intent),
                            tags=tags,
                        )
                    else:
                        raise ValueError(
                            f"unsupported legacy intent type: {intent.order_type.value}"
                        )
                    self.submit_order(order)

            def on_order_filled(self, event: object) -> None:
                factory.native_fill_transactions.append(_native_fill_transaction(event))
                if freqtrade_router is not None:
                    filled_at = datetime.fromtimestamp(
                        int(event.ts_event) / 1_000_000_000,
                        tz=timezone.utc,
                    )
                    _cash, equity, _realized_pnl, _unrealized_pnl, _positions = _native_portfolio_state(
                        self.portfolio,
                        factory.instrument_ids,
                        factory.quote_currency,
                        factory.venue,
                        factory.initial_cash,
                        filled_at,
                        Decimal(str(event.last_px)),
                    )
                    freqtrade_router.on_order_filled(
                        self,
                        event,
                        equity_after=equity,
                    )
                    return
                if btcqt_router is not None:
                    btcqt_router.on_order_filled(self, event)
                    return
                _dispatch_native_callback(factory.bridge.strategy, "on_native_order_filled", event)

            def on_order_accepted(self, event: object) -> None:
                if freqtrade_router is not None:
                    freqtrade_router.on_order_accepted(event)
                if btcqt_router is not None:
                    return

            def on_order_canceled(self, event: object) -> None:
                if freqtrade_router is not None:
                    freqtrade_router.on_order_canceled(self, event)
                    return
                if btcqt_router is not None:
                    btcqt_router.on_order_canceled(self, event)
                    return
                _dispatch_native_callback(
                    factory.bridge.strategy, "on_native_order_canceled", event
                )

            def on_order_rejected(self, event: object) -> None:
                if freqtrade_router is not None:
                    freqtrade_router.on_order_rejected(self, event)
                    return
                if btcqt_router is not None:
                    btcqt_router.on_order_rejected(self, event)
                    return
                _dispatch_native_callback(
                    factory.bridge.strategy, "on_native_order_rejected", event
                )

            def on_position_event(self, event: object) -> None:
                _dispatch_native_callback(
                    factory.bridge.strategy, "on_native_position_event", event
                )

        return AdaptedStrategy()


def _native_fill_transaction(event: object) -> NativeFillTransaction:
    """Capture raw fixed-precision native fill values for ledger reconciliation."""
    order_side = getattr(event, "order_side", None)
    side = getattr(order_side, "name", None)
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"unsupported native fill side: {order_side}")
    commission = getattr(event, "commission", None)
    return NativeFillTransaction(
        side=side,
        quantity=Decimal(str(event.last_qty)),
        price=Decimal(str(event.last_px)),
        commission=None if commission is None else str(commission),
    )


def _intent_quantity(
    intent: OrderIntent,
    bar: NativeBar,
    instrument: NativeInstrument,
    *,
    taker_fee: Decimal,
) -> object:
    raw = intent.base_quantity
    if raw is None:
        close = Decimal(str(bar.close))
        if close <= 0:
            raise ValueError("native bar close must be positive")
        if intent.quote_amount is not None:
            # The native bar matcher can consume the next price increment after
            # a partial fill. Reserve that increment and taker commission so a
            # cash account never enters borrowing merely due to execution.
            tick = instrument.price_increment.as_decimal()
            raw = intent.quote_amount / ((close + tick) * (Decimal("1") + taker_fee))
        else:
            raw = None
    if raw is None or raw <= 0:
        raise ValueError("intent must contain a positive order quantity")
    return instrument.make_qty(float(raw))


def _native_portfolio_state(
    portfolio: object,
    instrument_ids: Mapping[InstrumentKind, object],
    quote_currency: object,
    venue: object,
    initial_cash: Decimal,
    timestamp: datetime,
    mark_price: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Mapping[InstrumentKind, Decimal]]:
    """Read native portfolio/account objects; never synthesize fills or PnL."""
    native_portfolio = cast(NativePortfolio, portfolio)
    account = native_portfolio.account(venue=venue)
    balance = account.balance_free(quote_currency) if account is not None else None
    if balance is None:
        raise RuntimeError("native quote-currency balance is unavailable")
    cash = balance.as_decimal()
    spot = instrument_ids.get(InstrumentKind.SPOT)
    if spot is not None:
        native_spot = cast(NativeInstrument, spot)
        quote_total = account.balance_total(quote_currency)
        base_total = account.balance_total(native_spot.base_currency)
        if quote_total is None:
            raise RuntimeError("native spot quote-currency balance is unavailable")
        # The base balance is absent (rather than zero-valued) before the
        # first spot fill in Nautilus' cash account.
        base_value = Decimal("0") if base_total is None else base_total.as_decimal()
        equity = quote_total.as_decimal() + base_value * mark_price
        realized = Decimal("0")
        unrealized = Decimal("0")
    else:
        # Margin balances hold collateral; Nautilus reports marked PnL in the
        # quote currency separately, including realized execution costs.
        realized = _sum_native_money(
            native_portfolio.realized_pnls(venue=venue, target_currency=quote_currency),
        )
        unrealized = _sum_native_money(
            native_portfolio.unrealized_pnls(venue=venue, target_currency=quote_currency),
        )
        equity = initial_cash + realized + unrealized
    positions: dict[InstrumentKind, Decimal] = {}
    for kind, instrument in instrument_ids.items():
        native_instrument = cast(NativeInstrument, instrument)
        if kind is InstrumentKind.SPOT:
            # Cash-account sell orders must use currently sellable inventory,
            # not the gross portfolio position which can include locked base
            # reserved for an outstanding native order.
            available = account.balance_free(native_instrument.base_currency)
            positions[kind] = Decimal("0") if available is None else available.as_decimal()
        else:
            positions[kind] = native_portfolio.net_position(native_instrument.id)
    return cash, equity, realized, unrealized, positions


def _sum_native_money(values: Mapping[object, NativeMoney]) -> Decimal:
    return sum((money.as_decimal() for money in values.values()), start=Decimal("0"))


def _no_trade_cause(frame: pd.DataFrame, strategy: Strategy) -> str:
    if len(frame) <= strategy.metadata.warmup_bars:
        return "warmup_not_complete"
    return "strategy_returned_no_intent"


def _reduce_only(intent: OrderIntent) -> bool:
    """Spot sells consume inventory; perpetual sells can be valid short entries."""
    return intent.instrument is InstrumentKind.SPOT and intent.side is LegacyOrderSide.SELL


def _explanation(strategy: Strategy, context: object) -> tuple[tuple[str, object], ...]:
    explain = getattr(strategy, "explain_decision", None)
    if not callable(explain):
        return ()
    raw = explain(context)
    if not isinstance(raw, Mapping):
        raise ValueError("strategy explain_decision must return a mapping")
    return tuple(sorted((str(key), _json_safe(value)) for key, value in raw.items()))


def _json_safe(value: object) -> object:
    """Preserve source/Lab node values without admitting arbitrary objects."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("trace float values must be finite")
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("trace datetimes must be timezone-aware")
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    raise ValueError(f"trace value is not JSON-safe: {type(value).__name__}")


def _dispatch_native_callback(strategy: Strategy, name: str, event: object) -> None:
    callback = getattr(strategy, name, None)
    if callback is not None:
        if not callable(callback):
            raise ValueError(f"{name} must be callable")
        callback(event)
