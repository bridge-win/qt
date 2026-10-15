"""Credential-free image acceptance, invoked with network disabled by the shell runner."""

import os
import sqlite3
import sys
from pathlib import Path

from qt.btc_agent.broker import Quote
from qt.btc_agent.config import AgentConfig
from qt.btc_agent.paper import PaperBroker
from qt.btc_agent.service import Service
from qt.btc_agent.store import Store

now = 210 * 86_400_000
cfg = AgentConfig(mode="paper", state_dir=Path(sys.argv[2]))
store = Store(cfg.database)
broker = PaperBroker(store, lambda: Quote(50_000, 50_001, now))


def unavailable(*args: object) -> object:
    raise AssertionError("protective exit must not need candles or AI")


if sys.argv[1] == "crash":
    store.intent("image-crash-buy", "buy", .0004, 50_100, now)
    broker.submit("image-crash-buy", "buy", .0004, 50_100)
    # Abrupt process death after fill persistence, before position accounting.
    os._exit(72)

assert sys.argv[1] == "recover"
try:
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert len(store.pending()) == 1 and store.position().qty == 0
    with sqlite3.connect(cfg.state_dir / "backup.sqlite3") as backup:
        store.db.backup(backup)
    restored = Store(cfg.state_dir / "backup.sqlite3")
    try:
        assert len(restored.pending()) == 1
        assert restored.finish(PaperBroker(restored, broker.quote).lookup("image-crash-buy"))
        assert restored.position().qty == .0004
    finally:
        restored.close()
    execution = PaperBroker(store, lambda: Quote(40_000, 40_001, now))
    worker = Service(cfg, store, execution, unavailable, unavailable, lambda: now)
    result = worker.cycle()
    assert result["status"] == "executed" and result["reason"] == "stop_loss"
    assert not store.pending() and store.position().qty == 0
    assert store.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 2
    assert store.db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 2
    assert abs(execution.balances()["BTC"]) < 1e-10
    assert execution.balances()["USDT"] < 1000
    print("Offline paper image crash/reconcile/backup/protective-exit passed")
finally:
    store.close()
