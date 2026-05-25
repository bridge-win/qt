"""StateStore SQLite persistence tests."""

from __future__ import annotations

from datetime import datetime, timezone

from qt.core.types import Position
from qt.state import StateStore


def test_save_load_position(tmp_path) -> None:
    s = StateStore(tmp_path / "state.sqlite")
    p = Position(
        symbol="BTC/USDT", qty=0.1, avg_price=40_000,
        opened_ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
        stop_price=38_000, take_profit_price=46_000,
        time_stop_ts=datetime(2024, 1, 6, tzinfo=timezone.utc),
    )
    s.save_position(p)
    s.close()

    s2 = StateStore(tmp_path / "state.sqlite")
    q = s2.load_position("BTC/USDT")
    assert q is not None
    assert q.qty == 0.1
    assert q.avg_price == 40_000
    assert q.stop_price == 38_000
    assert q.opened_ts == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_kv_roundtrip(tmp_path) -> None:
    s = StateStore(tmp_path / "state.sqlite")
    s.kv_set("kill_switch_armed", True)
    s.kv_set("equity_peak", 105_000.5)
    s.kv_set("config", {"x": 1, "y": [1, 2, 3]})
    s.close()

    s2 = StateStore(tmp_path / "state.sqlite")
    assert s2.kv_get("kill_switch_armed") is True
    assert s2.kv_get("equity_peak") == 105_000.5
    assert s2.kv_get("config") == {"x": 1, "y": [1, 2, 3]}
    assert s2.kv_get("missing", "fallback") == "fallback"


def test_record_signal_and_order(tmp_path) -> None:
    s = StateStore(tmp_path / "state.sqlite")
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    s.record_signal(ts, "entry_long", 0.7, 0.4, {"price_rsi": 1.0}, ["price_rsi"])
    s.record_order(ts, "BTC/USDT", "buy", 0.1, 40_000, fee=2.0, venue="paper")
    cur = s.conn.execute("SELECT COUNT(*) FROM signals").fetchone()
    assert cur[0] == 1
    cur = s.conn.execute("SELECT side, qty FROM orders").fetchone()
    assert cur == ("buy", 0.1)
