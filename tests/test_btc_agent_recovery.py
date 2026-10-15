"""Offline process-death and SQLite backup acceptance; never contacts an exchange."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from qt.btc_agent.broker import Quote
from qt.btc_agent.config import AgentConfig
from qt.btc_agent.paper import PaperBroker
from qt.btc_agent.service import Service
from qt.btc_agent.store import Store

NOW = 210 * 86_400_000

# Exit without close(), finally blocks, or SQLite cleanup at durable boundaries.
CRASH = """
import os, sys
from pathlib import Path
from qt.btc_agent.broker import Quote
from qt.btc_agent.paper import PaperBroker
from qt.btc_agent.store import Store
store = Store(Path(sys.argv[1]))
broker = PaperBroker(store, lambda: Quote(50000, 50001, 18144000000))
store.intent('crash-buy', 'buy', .0004, 50100, 18144000000)
if sys.argv[2] == 'intent':
    os._exit(71)
fill = broker.submit('crash-buy', 'buy', .0004, 50100)
if sys.argv[2] == 'accepted':
    os._exit(72)
store.finish(fill)
with store.db:
    store.put('uncommitted', 'must roll back')
    os._exit(73)
"""


def unavailable(*args: object) -> object:
    raise AssertionError("offline recovery must not invoke a provider or reviewer")


@pytest.mark.parametrize("boundary,code", [("intent", 71), ("accepted", 72), ("accounted", 73)])
def test_abrupt_process_death_reconciles_without_resubmission(
    tmp_path: Path, boundary: str, code: int,
) -> None:
    cfg = AgentConfig(mode="paper", state_dir=tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("BTC_AGENT_")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.run(
        [sys.executable, "-c", CRASH, str(cfg.database), boundary],
        env=env, capture_output=True, timeout=20, check=False,
    )
    assert child.returncode == code, child.stderr.decode()
    store = Store(cfg.database)
    try:
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store.get("uncommitted") is None
        broker = PaperBroker(store, lambda: Quote(50_000, 50_001, NOW))
        worker = Service(cfg, store, broker, unavailable, unavailable, lambda: NOW)
        # Halt after restart: reconciliation still runs, no fresh order can be sent.
        cfg.kill_file.touch()
        for _ in range(3):
            result = worker.cycle()
            assert result["reason"] == ("unresolved_order" if boundary == "intent" else "kill_file")
        expected_fills = 0 if boundary == "intent" else 1
        assert store.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == expected_fills
        assert len(store.pending()) == (1 if boundary == "intent" else 0)
        assert store.position().qty == pytest.approx(expected_fills * .0004)
        assert store.position().cost == pytest.approx(expected_fills * 20.0204004)
        assert broker.balances()["BTC"] == pytest.approx(store.position().qty)
    finally:
        store.close()


def test_online_backup_restores_pending_fill_and_protective_exit(tmp_path: Path) -> None:
    source = Store(tmp_path / "source.sqlite3")
    backup = tmp_path / "restored" / "paper.sqlite3"
    backup.parent.mkdir()
    try:
        broker = PaperBroker(source, lambda: Quote(50_000, 50_001, NOW))
        source.intent("backup-buy", "buy", .0004, 50_100, NOW)
        broker.submit("backup-buy", "buy", .0004, 50_100)
        source.event(NOW, {"status": "pending"})
        # Snapshot while the source connection and WAL are still open.
        with sqlite3.connect(backup) as destination:
            source.db.backup(destination)
        restored = Store(backup)
        try:
            cfg = AgentConfig(mode="paper", state_dir=backup.parent)
            execution = PaperBroker(restored, lambda: Quote(40_000, 40_001, NOW))
            worker = Service(cfg, restored, execution, unavailable, unavailable, lambda: NOW)
            assert restored.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert len(restored.pending()) == 1
            result = worker.cycle()
            assert result["status"] == "executed"
            assert result["reason"] == "stop_loss"
            assert restored.pending() == []
            assert restored.position().qty == 0
            assert execution.balances()["BTC"] == pytest.approx(0)
            assert execution.balances()["USDT"] < 1000
            assert restored.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 2
            # Restore did not mutate the source or silently discard its pending intent.
            assert len(source.pending()) == 1
            assert source.position().qty == 0
        finally:
            restored.close()
    finally:
        source.close()
