"""Risk engine + sizing tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from qt.core.config import RiskConfig
from qt.core.types import Position, Signal, SignalKind
from qt.risk.engine import RiskEngine
from qt.risk.sizing import fractional_kelly, vol_target_size


def test_fractional_kelly_basic() -> None:
    # p=0.6, b=1.5 -> raw = (0.6*2.5 - 1)/1.5 = 0.333... ; quarter-Kelly = 0.083
    f = fractional_kelly(edge=1.0, win_rate=0.6, win_loss_ratio=1.5, fraction=0.25)
    assert 0.08 < f < 0.09


def test_fractional_kelly_no_edge() -> None:
    f = fractional_kelly(edge=1.0, win_rate=0.4, win_loss_ratio=1.0, fraction=0.5)
    assert f == 0.0


def test_vol_target() -> None:
    # rv=0.8, target=0.4 -> 0.5
    assert vol_target_size(0.8, 0.4) == pytest.approx(0.5)
    assert vol_target_size(0.0, 0.4) == 0.0


def test_risk_engine_blocks_on_kill_switch() -> None:
    eng = RiskEngine(cfg=RiskConfig(max_drawdown_pct=0.1))
    eng.state.equity_peak = 100.0
    sig = Signal(ts=datetime.now(tz=timezone.utc), kind=SignalKind.ENTRY_LONG, score=0.8,
                 target_quote_alloc=0.5)
    pos = Position(symbol="BTC/USDT")
    # equity dropped 20% -> > max DD -> block
    dec = eng.evaluate_entry(sig, equity=80.0, mark_price=50_000, atr_value=800,
                             realized_vol_annual=0.5, position=pos)
    assert dec.action == "block"
    assert dec.reason == "max_drawdown"


def test_risk_engine_opens_when_clean() -> None:
    eng = RiskEngine(cfg=RiskConfig(max_position_pct=0.2, kelly_fraction=0.5))
    sig = Signal(ts=datetime.now(tz=timezone.utc), kind=SignalKind.ENTRY_LONG, score=0.9,
                 target_quote_alloc=0.5)
    pos = Position(symbol="BTC/USDT")
    dec = eng.evaluate_entry(sig, equity=10_000, mark_price=40_000, atr_value=600,
                             realized_vol_annual=0.6, position=pos)
    assert dec.action == "open"
    assert dec.size_quote > 0
    assert dec.size_quote <= 10_000 * 0.2  # respects hard cap
    assert dec.stop_price is not None and dec.stop_price < 40_000
    assert dec.take_profit_price is not None and dec.take_profit_price > 40_000


def test_already_long_blocks() -> None:
    eng = RiskEngine(cfg=RiskConfig())
    sig = Signal(ts=datetime.now(tz=timezone.utc), kind=SignalKind.ENTRY_LONG, score=0.8,
                 target_quote_alloc=0.5)
    pos = Position(symbol="BTC/USDT", qty=0.1, avg_price=40_000)
    dec = eng.evaluate_entry(sig, equity=10_000, mark_price=40_000, atr_value=600,
                             realized_vol_annual=0.6, position=pos)
    assert dec.action == "block"
    assert dec.reason == "already_long"
