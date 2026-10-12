"""Crash-safe intents and inventory. Unknown submissions are never replayed."""

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from qt.btc_agent.broker import Fill

TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}


@dataclass
class Position:
    qty: float = 0
    cost: float = 0
    opened_ms: int = 0
    high_water: float = 0
    last_buy_ms: int = 0
    exiting: bool = False


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intents (
                client_id TEXT PRIMARY KEY, side TEXT NOT NULL,
                qty REAL NOT NULL, price REAL NOT NULL, created_ms INTEGER NOT NULL,
                complete INTEGER NOT NULL DEFAULT 0, fill TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_fills (client_id TEXT PRIMARY KEY, fill TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, payload TEXT NOT NULL
            );
        """)

    def get(self, key: str) -> object:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: object) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value, allow_nan=False))
        )

    def position(self) -> Position:
        value = self.get("position")
        return Position(**value) if isinstance(value, dict) else Position()

    def pending(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM intents WHERE complete=0 ORDER BY created_ms"))

    def intent(self, client_id: str, side: str, qty: float, price: float, now: int) -> None:
        with self.db:
            if self.pending():
                raise ValueError("an unresolved order blocks new submissions")
            self.db.execute(
                "INSERT INTO intents VALUES (?,?,?,?,?,0,NULL)", (client_id, side, qty, price, now)
            )

    def finish(self, fill: Fill) -> bool:
        if fill.status not in TERMINAL:
            return False
        with self.db:
            row = self.db.execute(
                "SELECT * FROM intents WHERE client_id=?", (fill.client_id,)
            ).fetchone()
            if row is None:
                raise ValueError("fill has no durable intent")
            if row["complete"]:
                return True
            numbers = (fill.filled_qty, fill.quote_qty, fill.fee_btc, fill.fee_quote)
            if any(not math.isfinite(n) or n < 0 for n in numbers):
                raise ValueError("invalid fill amounts")
            if fill.side.lower() != row["side"] or fill.filled_qty > row["qty"] + 1e-10:
                raise ValueError("fill differs from intent")
            if fill.filled_qty > 0 and fill.quote_qty <= 0:
                raise ValueError("fill is missing quote cost")
            pos = self.position()
            if fill.side.lower() == "buy" and fill.filled_qty:
                if fill.fee_btc >= fill.filled_qty:
                    raise ValueError("invalid base commission")
                if fill.quote_qty > fill.filled_qty * row["price"] * (1 + 1e-9):
                    raise ValueError("buy exceeded its limit")
                pos.qty += fill.filled_qty - fill.fee_btc
                pos.cost += fill.quote_qty + fill.fee_quote
                pos.opened_ms = pos.opened_ms or row["created_ms"]
                pos.last_buy_ms = row["created_ms"]
                pos.high_water = max(pos.high_water, fill.quote_qty / fill.filled_qty)
            elif fill.side.lower() == "sell" and fill.filled_qty:
                if fill.quote_qty < fill.filled_qty * row["price"] * (1 - 1e-9):
                    raise ValueError("sell violated its limit")
                sold = fill.filled_qty + fill.fee_btc
                if sold > pos.qty + 1e-10:
                    raise ValueError("sell exceeded owned inventory")
                pos.cost *= max(0, 1 - sold / pos.qty)
                pos.qty = max(0, pos.qty - sold)
                if pos.qty < 1e-10:
                    pos = Position(last_buy_ms=pos.last_buy_ms)
            self.put("position", asdict(pos))
            self.db.execute(
                "UPDATE intents SET complete=1,fill=? WHERE client_id=?",
                (json.dumps(asdict(fill)), fill.client_id),
            )
        return True

    def daily_buys(self, now_ms: int) -> float:
        start = now_ms // 86_400_000 * 86_400_000
        rows = self.db.execute(
            "SELECT fill FROM intents WHERE side='buy' AND created_ms>=? AND complete=1", (start,)
        )
        return sum(
            float(data["quote_qty"]) + float(data["fee_quote"])
            for row in rows
            if (data := json.loads(row[0]))
        )

    def event(self, now: int, payload: dict[str, object]) -> None:
        with self.db:
            self.put("heartbeat", {"ts": now, **payload})
            self.db.execute(
                "INSERT INTO events(ts,payload) VALUES (?,?)",
                (now, json.dumps(payload, allow_nan=False)),
            )
            # Keep the audit bounded; all order records remain durable.
            self.db.execute("DELETE FROM events WHERE id <= (SELECT MAX(id)-10000 FROM events)")

    def review(self, now: int, payload: dict[str, object]) -> None:
        with self.db:
            self.put("last_review", payload)
            self.db.execute(
                "INSERT INTO reviews(ts,payload) VALUES (?,?)",
                (now, json.dumps(payload, allow_nan=False)),
            )

    def close(self) -> None:
        self.db.close()
