from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest
from btc_backtest.engine.models import InstrumentKind, OrderIntent, OrderSide, OrderType
from btc_backtest.strategies.base import StrategyMetadata

from qt.legacy.btcqt.models import Intent, MarketState, Regime, Side
from qt.legacy.btcqt.models import Position as SourcePosition
from qt.nautilus.adapter import (
    BtcqtNativeIntentRouter,
    CausalStrategyBridge,
    FreqtradeNativeIntentRouter,
    _native_fee,
    _reduce_only,
)
from qt.strategy_ports.btcqt import (
    BTCQT_PORTS,
    BtcqtStrategyPort,
    CausalState,
    CausalStateTimeline,
    DataVersion,
    PortDecision,
    create_btcqt_port,
)
from qt.strategy_ports.freqtrade import (
    FreqtradeOrder,
    FreqtradeProtectionState,
    FreqtradeTradeState,
)


class _ProbeStrategy:
    metadata = StrategyMetadata(
        id="native_probe",
        version="1",
        description="Test strategy.",
        warmup_bars=0,
        supported_timeframes=("1h",),
    )

    def __init__(self) -> None:
        self.seen: list[pd.Timestamp] = []

    def on_bar(self, context: object) -> tuple[OrderIntent, ...]:
        self.seen.append(context.bars.index[-1])
        return (
            OrderIntent(
                instrument=InstrumentKind.SPOT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quote_amount=Decimal("10"),
                reason="causal_probe",
            ),
        )


def test_bridge_only_exposes_the_explicit_causal_history_end() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    index = pd.DatetimeIndex([start + timedelta(hours=offset) for offset in range(3)])
    frame = pd.DataFrame(
        {
            "open": [1, 2, 3],
            "high": [1, 2, 3],
            "low": [1, 2, 3],
            "close": [1, 2, 3],
            "volume": [1, 1, 1],
        },
        index=index,
    )
    strategy = _ProbeStrategy()
    bridge = CausalStrategyBridge(strategy, {})

    intents = bridge.decide(
        timestamp=start + timedelta(hours=1),
        history_end=index[0],
        bars=frame,
        cash=Decimal("100"),
        equity=Decimal("100"),
        positions={InstrumentKind.SPOT: Decimal("0")},
    )

    assert len(intents) == 1
    assert strategy.seen == [index[0]]
    assert bridge.traces[0].observed_rows == 1


def test_only_spot_sell_is_mapped_reduce_only() -> None:
    spot_sell = OrderIntent(
        instrument=InstrumentKind.SPOT,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        base_quantity=Decimal("1"),
        reason="spot_exit",
    )
    perpetual_sell = spot_sell.model_copy(update={"instrument": InstrumentKind.PERPETUAL})

    assert _reduce_only(spot_sell) is True
    assert _reduce_only(perpetual_sell) is False


def test_btcqt_fill_fee_is_quote_normalized_or_fails_without_fx() -> None:
    assert _native_fee(
        "1.25 USDT",
        quote_currency="USDT",
        base_currency="BTC",
        fill_price=Decimal("100"),
    ) == 1.25
    assert _native_fee(
        "0.001 BTC",
        quote_currency="USDT",
        base_currency="BTC",
        fill_price=Decimal("100"),
    ) == 0.1
    with pytest.raises(ValueError, match="fill-time FX"):
        _native_fee(
            "1 BNB",
            quote_currency="USDT",
            base_currency="BTC",
            fill_price=Decimal("100"),
        )


def test_trace_preserves_json_safe_typed_source_explanations() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [0.0]},
        index=pd.DatetimeIndex([start]),
    )

    class _ExplainedProbe(_ProbeStrategy):
        def on_bar(self, context: object) -> tuple[OrderIntent, ...]:
            del context
            return ()

        @staticmethod
        def explain_decision(context: object) -> dict[str, object]:
            del context
            return {"rsi": 42.5, "entry": False, "nodes": {"trend": "pass"}}

    bridge = CausalStrategyBridge(_ExplainedProbe(), {})
    bridge.decide(
        timestamp=start,
        bars=frame,
        cash=Decimal("100"),
        equity=Decimal("100"),
        positions={InstrumentKind.SPOT: Decimal("0")},
    )

    observed = dict(bridge.traces[0].observed_values)
    assert observed["rsi"] == 42.5
    assert observed["entry"] is False
    assert observed["nodes"] == {"trend": "pass"}


@pytest.mark.native_engine
def test_freqtrade_router_preserves_partial_fill_stop_replace_and_protection_lifecycle() -> None:
    pytest.importorskip("nautilus_trader")
    from nautilus_trader.model import OrderSide as NativeOrderSide

    observed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)

    class _Port:
        metadata = SimpleNamespace(strategy_id="btc_quant_catalog")

        def __init__(self) -> None:
            self.accepted: list[str] = []
            self.closed: list[tuple[datetime, float]] = []

        def on_fill(self, order: object, **kwargs: object) -> FreqtradeTradeState:
            assert isinstance(order, FreqtradeOrder)
            if order.action == "enter_long":
                return FreqtradeTradeState(
                    100.0,
                    100.0,
                    95.0,
                    95.0,
                    1.0,
                    observed_at,
                )
            return FreqtradeTradeState(
                100.0,
                0.0,
                95.0,
                95.0,
                0.0,
                observed_at,
            )

        @staticmethod
        def initial_stop_order(
            trade: FreqtradeTradeState,
            *,
            submitted_at: datetime,
        ) -> FreqtradeOrder:
            del submitted_at
            return FreqtradeOrder("replace_stop", trade.remaining_quantity, 95.0, "initial", order_id="stop-1")

        def on_stop_accepted(
            self,
            trade: FreqtradeTradeState,
            order: FreqtradeOrder,
        ) -> FreqtradeTradeState:
            self.accepted.append(order.order_id)
            return trade

        def on_trade_closed(
            self,
            state: FreqtradeProtectionState,
            *,
            closed_at: datetime,
            equity_after: float,
        ) -> FreqtradeProtectionState:
            self.closed.append((closed_at, equity_after))
            return state

    class _Instrument:
        id = "spot"
        base_currency = "BTC"
        quote_currency = "USDT"

        @staticmethod
        def make_qty(value: float) -> float:
            return value

        @staticmethod
        def make_price(value: float) -> float:
            return value

    class _Factory:
        def __init__(self) -> None:
            self.index = 0

        def market(self, **kwargs: object) -> object:
            return self._order(**kwargs)

        def stop_market(self, **kwargs: object) -> object:
            return self._order(**kwargs)

        def _order(self, **kwargs: object) -> object:
            self.index += 1
            return SimpleNamespace(client_order_id=f"native-{self.index}", **kwargs)

    class _Strategy:
        def __init__(self) -> None:
            self.order_factory = _Factory()
            self.submitted: list[object] = []
            self.cancelled: list[object] = []
            self.cache = None

        def submit_order(self, order: object) -> None:
            self.submitted.append(order)

        def cancel_order(self, client_order_id: object) -> None:
            self.cancelled.append(client_order_id)

    port = _Port()
    router = FreqtradeNativeIntentRouter(port, instrument=_Instrument())
    strategy = _Strategy()
    enter = FreqtradeOrder("enter_long", 1.0, 95.0, "entry", order_id="enter-1")
    router.submit_decision(
        strategy,
        SimpleNamespace(orders=(enter,), protection_state=FreqtradeProtectionState()),
    )
    entry_native = strategy.submitted[0]
    router.on_order_filled(
        strategy,
        SimpleNamespace(
            client_order_id=entry_native.client_order_id,
            trade_id="entry-fill",
            ts_event=int(observed_at.timestamp() * 1_000_000_000),
            last_px=100.0,
            last_qty=1.0,
            commission="0.01 USDT",
        ),
        equity_after=Decimal("999.99"),
    )
    first_stop = strategy.submitted[1]
    router.on_order_accepted(SimpleNamespace(client_order_id=first_stop.client_order_id))
    assert port.accepted == ["stop-1"]

    replacement = FreqtradeOrder("replace_stop", 1.0, 96.0, "trail", order_id="stop-2")
    router.submit_decision(
        strategy,
        SimpleNamespace(orders=(replacement,), protection_state=FreqtradeProtectionState()),
    )
    assert strategy.cancelled == [first_stop.client_order_id]
    router.on_order_canceled(strategy, SimpleNamespace(client_order_id=first_stop.client_order_id))
    assert strategy.submitted[-1].trigger_price == 96.0

    exit_ = FreqtradeOrder("exit_long", 1.0, None, "exit", order_id="exit-1")
    router.submit_decision(
        strategy,
        SimpleNamespace(orders=(exit_,), protection_state=FreqtradeProtectionState()),
    )
    exit_native = strategy.submitted[-1]
    assert exit_native.order_side is NativeOrderSide.SELL
    router.on_order_filled(
        strategy,
        SimpleNamespace(
            client_order_id=exit_native.client_order_id,
            trade_id="exit-fill",
            ts_event=int((observed_at + timedelta(hours=1)).timestamp() * 1_000_000_000),
            last_px=101.0,
            last_qty=1.0,
            commission="0.01 USDT",
        ),
        equity_after=Decimal("1000.98"),
    )
    assert port.closed == [(observed_at + timedelta(hours=1), 1000.98)]


@pytest.mark.native_engine
@pytest.mark.parametrize(
    ("entry_side", "protective_side", "expected_instrument"),
    (("BUY", "SELL", "perpetual"), ("SELL", "BUY", "perpetual")),
)
def test_btcqt_router_retains_partial_entry_and_routes_protection_by_position(
    entry_side: str,
    protective_side: str,
    expected_instrument: str,
) -> None:
    """Each native fill reaches the source hook on the one USDT-M instrument."""
    nautilus = pytest.importorskip("nautilus_trader")
    from nautilus_trader.model import OrderSide as NativeOrderSide

    del nautilus
    state = _source_state()

    class _Source:
        name = "router_probe"

        def __init__(self) -> None:
            self.pos = SourcePosition(strategy=self.name)
            self.fills: list[object] = []

        def on_fill(self, fill: object, market: object) -> tuple[list[Intent], float]:
            del market
            self.fills.append(fill)
            self.pos.side = Side.BUY if entry_side == "BUY" else Side.SELL
            return (
                [
                    Intent(
                        action="PLACE_TP",
                        strategy=self.name,
                        side=Side(protective_side),
                        price=105.0,
                        qty=0.25,
                        reason="source_tp",
                    )
                ],
                0.0,
            )

    class _Instrument:
        def __init__(self, name: str) -> None:
            self.id = name
            self.base_currency = "BTC"
            self.quote_currency = "USDT"

        @staticmethod
        def make_qty(value: float) -> float:
            return value

        @staticmethod
        def make_price(value: float) -> float:
            return value

    class _OrderFactory:
        def __init__(self) -> None:
            self.index = 0

        def market(self, **kwargs: object) -> object:
            return self._order(**kwargs)

        def limit(self, **kwargs: object) -> object:
            return self._order(**kwargs)

        def stop_market(self, **kwargs: object) -> object:
            return self._order(**kwargs)

        def _order(self, **kwargs: object) -> object:
            self.index += 1
            return SimpleNamespace(client_order_id=object(), side=kwargs["order_side"], **kwargs)

    class _Strategy:
        def __init__(self) -> None:
            self.order_factory = _OrderFactory()
            self.submitted: list[object] = []
            self.cancelled: list[object] = []
            self.cache = None

        def submit_order(self, order: object) -> None:
            self.submitted.append(order)

        def cancel_order(self, client_order_id: object) -> None:
            self.cancelled.append(client_order_id)

        def cancel_all_orders(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

    source = _Source()
    port = BtcqtStrategyPort(BTCQT_PORTS["btcqt_s0_trend"], source)
    strategy = _Strategy()
    perpetual = _Instrument("perpetual")
    router = BtcqtNativeIntentRouter(port, perpetual_instrument=perpetual)
    entry = PortDecision(
        strategy_id="btcqt_s0_trend",
        timestamp=state.available_at,
        state=state,
        intents=(
            Intent(
                action="MARKET_ENTER",
                strategy=source.name,
                side=Side(entry_side),
                qty=1.0,
                reason="source_entry",
            ),
        ),
    )
    router.submit_decision(strategy, entry)

    assert strategy.submitted[0].instrument_id == expected_instrument
    order_id = strategy.submitted[0].client_order_id
    for trade_id in ("partial-1", "partial-2"):
        router.on_order_filled(
            strategy,
            SimpleNamespace(
                client_order_id=order_id,
                trade_id=trade_id,
                order_side=NativeOrderSide.BUY
                if entry_side == "BUY"
                else NativeOrderSide.SELL,
                    ts_event=int(state.observed_at.timestamp() * 1_000_000_000),
                last_px=100.0,
                last_qty=0.25,
                commission=None,
            ),
        )

    assert len(source.fills) == 2
    assert [order.instrument_id for order in strategy.submitted[1:]] == [expected_instrument]
    # The source gateway replaces a TP after a partial fill; native submission
    # waits for cancellation so two reduce-only protections never coexist.
    assert strategy.cancelled == [strategy.submitted[1].client_order_id]
    router.submit_decision(
        strategy,
        PortDecision(
            strategy_id="btcqt_s0_trend",
            timestamp=state.available_at,
            state=state,
            intents=(Intent(action="CANCEL_ENTRIES", strategy=source.name),),
        ),
    )
    assert strategy.cancelled == [strategy.submitted[1].client_order_id, order_id]


@pytest.mark.native_engine
def test_btcqt_delayed_fill_uses_latest_causal_anchor_not_submission_snapshot() -> None:
    """S1 ladder TP must use the state known at the fill, not order entry."""

    from nautilus_trader.model import OrderSide as NativeOrderSide

    initial = _source_state()
    initial.state.price = 100.0
    initial.state.ema_anchor = 100.0
    initial.state.atr = 2.0
    initial.state.sigma_1m = 0.001
    initial.state.regime = Regime.RANGE
    initial.state.warmed_up = True
    later_at = initial.observed_at + timedelta(minutes=1)
    later = CausalState(
        state=MarketState(
            ts=int(later_at.timestamp() * 1000),
            price=101.0,
            ema_anchor=130.0,
            atr=2.0,
            sigma_1m=0.001,
            regime=Regime.RANGE,
            warmed_up=True,
        ),
        observed_at=later_at,
        available_at=later_at,
        inputs=(DataVersion("ohlcv_1m", "sha256:test", later_at),),
    )

    class _Instrument:
        id = "perpetual"
        base_currency = "BTC"
        quote_currency = "USDT"

        @staticmethod
        def make_qty(value: float) -> float:
            return value

        @staticmethod
        def make_price(value: float) -> float:
            return value

    class _Factory:
        def __init__(self) -> None:
            self._sequence = 0

        def market(self, **kwargs: object) -> object:
            return self._order(**kwargs)

        def limit(self, **kwargs: object) -> object:
            return self._order(**kwargs)

        def stop_market(self, **kwargs: object) -> object:
            return self._order(**kwargs)

        def _order(self, **kwargs: object) -> object:
            self._sequence += 1
            return SimpleNamespace(
                client_order_id=f"order-{self._sequence}",
                side=kwargs["order_side"],
                **kwargs,
            )

    class _Strategy:
        def __init__(self) -> None:
            self.order_factory = _Factory()
            self.cache = None
            self.submitted: list[object] = []
            self.cancelled: list[object] = []

        def submit_order(self, order: object) -> None:
            self.submitted.append(order)

        def cancel_order(self, client_order_id: object) -> None:
            self.cancelled.append(client_order_id)

        @staticmethod
        def cancel_all_orders(*args: object, **kwargs: object) -> None:
            del args, kwargs

    port = create_btcqt_port("btcqt_s1_wick_ladder")
    router = BtcqtNativeIntentRouter(port, perpetual_instrument=_Instrument())
    strategy = _Strategy()
    armed = port.decide(
        CausalStateTimeline((initial,)),
        timestamp=initial.available_at,
        equity=10_000.0,
    )
    router.submit_decision(strategy, armed)
    entry = next(order for order in strategy.submitted if order.order_side is NativeOrderSide.BUY)
    # No replacement order is needed: this records the completed state that
    # became available while the earlier market order remained unfilled.
    router.submit_decision(
        strategy,
        PortDecision("btcqt_s1_wick_ladder", later.available_at, later, ()),
    )
    router.on_order_filled(
        strategy,
        SimpleNamespace(
            client_order_id=entry.client_order_id,
            trade_id="delayed-fill",
            order_side=NativeOrderSide.BUY,
            ts_event=int(later_at.timestamp() * 1_000_000_000),
            last_px=float(entry.price),
            last_qty=float(entry.quantity),
            commission=None,
        ),
    )

    take_profit = next(order for order in strategy.submitted if "tp" in order.tags)
    expected = float(entry.price) + 0.4 * (130.0 - float(entry.price))
    assert float(take_profit.price) == pytest.approx(expected)


def _source_state() -> CausalState:
    observed_at = pd.Timestamp("2025-01-01T00:01:00Z")
    return CausalState(
        state=MarketState(ts=int(observed_at.timestamp() * 1000)),
        observed_at=observed_at.to_pydatetime(),
        available_at=observed_at.to_pydatetime(),
        inputs=(
            DataVersion(
                dataset_id="ohlcv_1m",
                version="sha256:test",
                available_at=observed_at.to_pydatetime(),
            ),
        ),
    )
