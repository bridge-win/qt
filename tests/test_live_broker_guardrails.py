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
    LiveTradingDisabledError,
    UnsafeApiKeyError,
)


class FakeClient:
    """Minimal ccxt-like client for tests."""

    def __init__(self, *, can_withdraw: bool = False, balance: dict | None = None):
        self._can_withdraw = can_withdraw
        self._balance = balance or {"free": {"USDT": 1000.0}, "total": {"BTC": 0.0}}
        self.orders: list[dict] = []

    def sapiGetAccountApirestrictions(self) -> dict:  # noqa: N802 (mirror ccxt name)
        return {"enableWithdrawals": self._can_withdraw}

    def create_order(self, **kwargs) -> dict:
        self.orders.append(kwargs)
        amount = kwargs["amount"]
        return {"average": 50_000.0, "filled": amount, "fee": {"cost": 0.5}}

    def fetch_balance(self) -> dict:
        return self._balance


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
    trade = broker.submit(_order(qty=0.001), mark_price=50_000.0)
    assert len(fake.orders) == 1
    assert trade.venue == "binance"
    assert trade.price == 50_000.0
    assert trade.fee == 0.5
