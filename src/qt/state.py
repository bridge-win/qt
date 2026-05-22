"""Persistent state for the paper / live engine.

Uses SQLite (stdlib only) — a single file under `data/state.sqlite`.
Tables:

- `positions` — current open position per symbol
- `orders` — submitted orders & their fills
- `equity` — equity history (one row per cycle)
- `signals` — emitted signals with reasons (for audit)
- `kv` — generic key/value bag (cooldown timestamps, kill-switch state)

The engine should call `flush()` at the end of each cycle. A crash-restart
loads state via `load_position` / `load_kv`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from qt.core.types import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty REAL NOT NULL,
    avg_price REAL NOT NULL,
    opened_ts TEXT,
    stop_price REAL,
    take_profit_price REAL,
    time_stop_ts TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL,
    venue TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    position_value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    score REAL NOT NULL,
    target_alloc REAL,
    factors TEXT,
    reasons TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------ Positions ----------------------------------------------------

    def save_position(self, p: Position) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO positions(symbol, qty, avg_price, opened_ts, "
            "stop_price, take_profit_price, time_stop_ts) VALUES (?,?,?,?,?,?,?)",
            (
                p.symbol, p.qty, p.avg_price,
                p.opened_ts.isoformat() if p.opened_ts else None,
                p.stop_price, p.take_profit_price,
                p.time_stop_ts.isoformat() if p.time_stop_ts else None,
            ),
        )

    def load_position(self, symbol: str) -> Position | None:
        row = self.conn.execute(
            "SELECT qty, avg_price, opened_ts, stop_price, take_profit_price, "
            "time_stop_ts FROM positions WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if not row:
            return None
        qty, avg, opened, stop, tp, ts_stop = row
        return Position(
            symbol=symbol, qty=qty, avg_price=avg,
            opened_ts=datetime.fromisoformat(opened) if opened else None,
            stop_price=stop, take_profit_price=tp,
            time_stop_ts=datetime.fromisoformat(ts_stop) if ts_stop else None,
        )

    def delete_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))

    # ------ Orders -------------------------------------------------------

    def record_order(
        self, ts: datetime, symbol: str, side: str, qty: float, price: float,
        fee: float = 0.0, venue: str = "paper", note: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO orders(ts, symbol, side, qty, price, fee, venue, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts.isoformat(), symbol, side, qty, price, fee, venue, note),
        )

    # ------ Equity -------------------------------------------------------

    def record_equity(
        self, ts: datetime, equity: float, cash: float, position_value: float
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity(ts, equity, cash, position_value) "
            "VALUES (?,?,?,?)",
            (ts.isoformat(), equity, cash, position_value),
        )

    # ------ Signals ------------------------------------------------------

    def record_signal(
        self, ts: datetime, kind: str, score: float, target_alloc: float,
        factors: dict[str, float], reasons: list[str],
    ) -> None:
        self.conn.execute(
            "INSERT INTO signals(ts, kind, score, target_alloc, factors, reasons) "
            "VALUES (?,?,?,?,?,?)",
            (
                ts.isoformat(), kind, score, target_alloc,
                json.dumps(factors), json.dumps(reasons),
            ),
        )

    # ------ KV -----------------------------------------------------------

    def kv_set(self, key: str, value: object) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)",
            (key, json.dumps(value, default=str)),
        )

    def kv_get(self, key: str, default: object | None = None) -> object | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row[0])

    def ts_now(self) -> str:
        return datetime.now(tz=timezone.utc).isoformat()
