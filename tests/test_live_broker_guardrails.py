"""LiveBroker safety-gate tests. No network — a fake ccxt client is injected."""

from __future__ import annotations

from pathlib import Path

import pytest

from qt.core.config import ExecutionConfig
from qt.core.types import OrderSide, OrderType
from qt.execution.base import Order
from qt.execution.live import (
    GuardrailBlockedError,
    LiveBroker,
    LiveOrderRejectedError,
    LiveTradingDisabledError,
    UnsafeApiKeyError,
)


class FakeClient:
    """Minimal ccxt-like client for tests."""

    def __init__(
        self,
        *,
        can_withdraw: bool = False,
        balance: dict[str, object] | None = None,
    ) -> None:
        self._can_withdraw = can_withdraw
        self._balance = balance or {"free": {"USDT": 1000.0}, "total": {"BTC": 0.0}}
        self.orders: list[dict[str, object]] = []
        self.markets = {
            "BTC/USDT": {
                "spot": True,
                "limits": {"amount": {"min": 0.00001}, "cost": {"min": 10.0}},
            }
        }

    def sapiGetAccountApirestrictions(self) -> dict[str, object]:  # noqa: N802 (mirror ccxt name)
        return {"enableWithdrawals": self._can_withdraw}

    def create_order(self, **kwargs: object) -> dict[str, object]:
        self.orders.append(kwargs)
        amount = kwargs["amount"]
        return {"average": 50_000.0, "filled": amount, "fee": {"cost": 0.5}}

    def fetch_balance(self) -> dict[str, object]:
        return self._balance

    def load_markets(self) -> object:
        return self.markets


def _order(qty: float = 0.001, side: OrderSide = OrderSide.BUY) -> Order:
    return Order(symbol="BTC/USDT", side=side, type=OrderType.MARKET, qty=qty)


def test_disabled_by_default_blocks() -> None:
    cfg = ExecutionConfig()  # live_enabled False
    broker = LiveBroker(cfg=cfg, _client=FakeClient())
    with pytest.raises(LiveTradingDisabledError):
        broker.submit(_order(), mark_price=50_000.0)


def test_unsafe_key_refused_at_construction() -> None:
    cfg = ExecutionConfig(live_enabled=True, require_trade_only_key=True)
    with pytest.raises(UnsafeApiKeyError, match="WITHDRAWAL"):
        LiveBroker(cfg=cfg, _client=FakeClient(can_withdraw=True))


def test_trade_only_key_allowed() -> None:
    cfg = ExecutionConfig(live_enabled=True, dry_run=True)
    broker = LiveBroker(cfg=cfg, _client=FakeClient(can_withdraw=False))
    # dry-run submit returns a paper trade, sends nothing
    trade = broker.submit(_order(), mark_price=50_000.0)
    assert trade.venue == "paper"
    assert "DRY_RUN" in trade.note


def test_kill_file_blocks(tmp_path: Path) -> None:
    kill = tmp_path / "KILL"
    kill.write_text("stop")
    cfg = ExecutionConfig(live_enabled=True, dry_run=True, kill_file=str(kill))
    broker = LiveBroker(cfg=cfg, _client=FakeClient())
    with pytest.raises(GuardrailBlockedError, match="KILL"):
        broker.submit(_order(), mark_price=50_000.0)


def test_max_order_quote_enforced() -> None:
    cfg = ExecutionConfig(live_enabled=True, dry_run=True, max_order_quote=10.0)
    broker = LiveBroker(cfg=cfg, _client=FakeClient())
    # 0.001 BTC * 50000 = 50 USDT > 10 cap
    with pytest.raises(GuardrailBlockedError, match="max_order_quote"):
        broker.submit(_order(qty=0.001), mark_price=50_000.0)


def test_daily_spend_cap_enforced() -> None:
    # Use the real (non-dry-run) path so daily spend is actually tracked.
    cfg = ExecutionConfig(
        live_enabled=True, dry_run=False,
        max_order_quote=1000.0, max_daily_spend_quote=60.0,
        max_total_exposure_quote=100_000.0, require_trade_only_key=False,
    )
    broker = LiveBroker(cfg=cfg, _client=FakeClient())
    broker.submit(_order(qty=0.001), mark_price=50_000.0)  # 50 spent
    with pytest.raises(GuardrailBlockedError, match="daily spend"):
        broker.submit(_order(qty=0.001), mark_price=50_000.0)  # +50 = 100 > 60


def test_symbol_allowlist() -> None:
    cfg = ExecutionConfig(live_enabled=True, dry_run=True, symbol="BTC/USDT")
    broker = LiveBroker(cfg=cfg, _client=FakeClient())
    bad = Order(symbol="ETH/USDT", side=OrderSide.BUY, type=OrderType.MARKET, qty=0.001)
    with pytest.raises(GuardrailBlockedError, match="not allowed"):
        broker.submit(bad, mark_price=50_000.0)


def test_real_order_sends_and_tracks_spend() -> None:
    cfg = ExecutionConfig(
        live_enabled=True, dry_run=False, require_trade_only_key=False,
        max_order_quote=1000.0, max_daily_spend_quote=1000.0,
        max_total_exposure_quote=100_000.0,
    )
    fake = FakeClient()
    broker = LiveBroker(cfg=cfg, _client=fake)
    trade = broker.submit(
        Order(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            qty=0.001,
            client_id="qt-test-order",
        ),
        mark_price=50_000.0,
    )
    assert len(fake.orders) == 1
    assert trade.venue == "binance"
    assert trade.price == 50_000.0
    assert trade.fee == 0.5
    assert fake.orders[0]["params"] == {"newClientOrderId": "qt-test-order"}


def test_live_order_uses_venue_specific_client_order_id() -> None:
    cfg = ExecutionConfig(
        live_enabled=True,
        dry_run=False,
        require_trade_only_key=False,
        max_order_quote=1000.0,
        max_daily_spend_quote=1000.0,
        max_total_exposure_quote=100_000.0,
        venue="okx",
    )
    fake = FakeClient()
    broker = LiveBroker(cfg=cfg, _client=fake)

    broker.submit(
        Order(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            qty=0.001,
            client_id="qt-okx-order",
        ),
        mark_price=50_000.0,
    )

    assert fake.orders[0]["params"] == {"clOrdId": "qt-okx-order"}


def test_rejects_non_positive_order_inputs() -> None:
    cfg = ExecutionConfig(live_enabled=True, dry_run=True)
    broker = LiveBroker(cfg=cfg, _client=FakeClient())
    with pytest.raises(GuardrailBlockedError, match="positive finite quantity"):
        broker.submit(_order(qty=0.0), mark_price=50_000.0)
    with pytest.raises(GuardrailBlockedError, match="positive finite mark_price"):
        broker.submit(_order(qty=0.001), mark_price=float("nan"))


def test_live_buy_checks_available_cash_before_submit() -> None:
    cfg = ExecutionConfig(
        live_enabled=True,
        dry_run=False,
        require_trade_only_key=False,
        max_order_quote=1000.0,
        max_daily_spend_quote=1000.0,
        max_total_exposure_quote=100_000.0,
    )
    fake = FakeClient(balance={"free": {"USDT": 25.0}, "total": {"BTC": 0.0}})
    broker = LiveBroker(cfg=cfg, _client=fake)

    with pytest.raises(GuardrailBlockedError, match="available USDT"):
        broker.submit(_order(qty=0.001), mark_price=50_000.0)

    assert fake.orders == []


def test_live_order_respects_exchange_min_cost() -> None:
    cfg = ExecutionConfig(
        live_enabled=True,
        dry_run=False,
        require_trade_only_key=False,
        max_order_quote=1000.0,
        max_daily_spend_quote=1000.0,
        max_total_exposure_quote=100_000.0,
    )
    fake = FakeClient()
    broker = LiveBroker(cfg=cfg, _client=fake)

    with pytest.raises(GuardrailBlockedError, match="minimum notional"):
        broker.submit(_order(qty=0.0001), mark_price=50_000.0)

    assert fake.orders == []


def test_live_order_rejects_unfilled_exchange_response() -> None:
    class UnfilledClient(FakeClient):
        def create_order(self, **kwargs: object) -> dict[str, object]:
            self.orders.append(kwargs)
            return {"status": "canceled", "filled": 0, "average": None}

    cfg = ExecutionConfig(
        live_enabled=True,
        dry_run=False,
        require_trade_only_key=False,
        max_order_quote=1000.0,
        max_daily_spend_quote=1000.0,
        max_total_exposure_quote=100_000.0,
    )
    fake = UnfilledClient()
    broker = LiveBroker(cfg=cfg, _client=fake)

    with pytest.raises(LiveOrderRejectedError, match="no fill"):
        broker.submit(_order(qty=0.001), mark_price=50_000.0)


def test_live_order_rejects_missing_fill_quantity() -> None:
    class MissingFillClient(FakeClient):
        def create_order(self, **kwargs: object) -> dict[str, object]:
            self.orders.append(kwargs)
            return {"status": "closed", "average": 50_000.0}

    cfg = ExecutionConfig(
        live_enabled=True,
        dry_run=False,
        require_trade_only_key=False,
        max_order_quote=1000.0,
        max_daily_spend_quote=1000.0,
        max_total_exposure_quote=100_000.0,
    )
    broker = LiveBroker(cfg=cfg, _client=MissingFillClient())

    with pytest.raises(LiveOrderRejectedError, match="fill quantity"):
        broker.submit(_order(qty=0.001), mark_price=50_000.0)


def test_live_order_rejects_missing_fill_price() -> None:
    class MissingPriceClient(FakeClient):
        def create_order(self, **kwargs: object) -> dict[str, object]:
            self.orders.append(kwargs)
            return {"status": "closed", "filled": 0.001}

    cfg = ExecutionConfig(
        live_enabled=True,
        dry_run=False,
        require_trade_only_key=False,
        max_order_quote=1000.0,
        max_daily_spend_quote=1000.0,
        max_total_exposure_quote=100_000.0,
    )
    broker = LiveBroker(cfg=cfg, _client=MissingPriceClient())

    with pytest.raises(LiveOrderRejectedError, match="fill price"):
        broker.submit(_order(qty=0.001), mark_price=50_000.0)


def test_live_sell_checks_available_position_before_submit() -> None:
    cfg = ExecutionConfig(
        live_enabled=True,
        dry_run=False,
        require_trade_only_key=False,
        max_order_quote=1000.0,
        max_daily_spend_quote=1000.0,
        max_total_exposure_quote=100_000.0,
    )
    fake = FakeClient(balance={"free": {"USDT": 1000.0}, "total": {"BTC": 0.0005}})
    broker = LiveBroker(cfg=cfg, _client=fake)

    with pytest.raises(GuardrailBlockedError, match="available BTC"):
        broker.submit(_order(qty=0.001, side=OrderSide.SELL), mark_price=50_000.0)

    assert fake.orders == []
