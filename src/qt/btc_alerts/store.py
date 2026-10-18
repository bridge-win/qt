"""Transactional alert ledger. A send claim and its hourly quota are indivisible."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .knowledge import CARDS, SOURCES, strategy_cards
from .models import Candidate, Policy, digest


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False)


class ConflictError(ValueError):
    """Optimistic policy or immutable event conflict."""


MAX_EVENTS = 10000


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "alerts.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS policy (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL, payload TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS policy_history (
                    revision INTEGER PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL,
                    review TEXT, outcome TEXT);
                CREATE TABLE IF NOT EXISTS review_batches (
                    batch_id TEXT PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    next_retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL, payload TEXT NOT NULL, message_id INTEGER,
                    error_code TEXT, is_test INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS quota (
                    recipient_key TEXT PRIMARY KEY, next_allowed_at REAL NOT NULL,
                    previous_allowed_at REAL NOT NULL, last_seen_at REAL NOT NULL, owner TEXT);
                CREATE TABLE IF NOT EXISTS validations (
                    strategy_version TEXT NOT NULL, strategy_id TEXT NOT NULL, direction TEXT NOT NULL,
                    created_at REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(strategy_version,strategy_id,direction));
                CREATE TABLE IF NOT EXISTS cards (strategy_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS card_search USING fts5(strategy_id UNINDEXED, text);
                CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status,next_retry_at);
                CREATE INDEX IF NOT EXISTS event_created ON events(created_at);
            """)
            now = time.time()
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO policy VALUES (1,1,?,?)", (encode(Policy().model_dump()), now))
            db.execute("INSERT OR IGNORE INTO policy_history VALUES (1,?,?)", (encode(Policy().model_dump()), now))
            cards = strategy_cards()
            knowledge_hash = digest(cards)
            previous = db.execute("SELECT payload FROM runtime WHERE key='knowledge_hash'").fetchone()
            if previous is None or json.loads(previous[0]) != knowledge_hash:
                for card in cards:
                    db.execute("INSERT OR REPLACE INTO cards VALUES (?,?)", (card["strategy_id"], encode(card)))
                db.execute("DELETE FROM card_search")
                db.executemany("INSERT INTO card_search VALUES (?,?)", [
                    (card["strategy_id"], encode(card)) for card in cards
                ])
                db.execute("INSERT OR REPLACE INTO runtime VALUES ('knowledge_hash',?)", (encode(knowledge_hash),))
            db.commit()
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    def policy(self) -> tuple[int, Policy]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM policy WHERE singleton=1").fetchone()
            return int(row["revision"]), Policy.model_validate_json(row["payload"])

    def set_policy(self, expected_revision: int, policy: Policy, now: float) -> int:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision,payload FROM policy WHERE singleton=1").fetchone()
            if current["revision"] != expected_revision:
                db.rollback()
                raise ConflictError("policy_revision_conflict")
            revision = expected_revision + 1
            db.execute("UPDATE policy SET revision=?,payload=?,updated_at=? WHERE singleton=1", (revision, encode(policy.model_dump()), now))
            db.execute("INSERT INTO policy_history VALUES (?,?,?)", (revision, encode(policy.model_dump()), now))
            db.commit()
        return revision

    def put(self, key: str, value: Any) -> None:
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO runtime VALUES (?,?)", (key, encode(value)))

    def get(self, key: str, default: Any = None) -> Any:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM runtime WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def add_candidate(self, candidate: Candidate, now: float) -> bool:
        with self.connection() as db:
            if db.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= MAX_EVENTS:
                raise ValueError("alert_event_capacity")
            cursor = db.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,'queued','',?,NULL,NULL)",
                (candidate.candidate_id, now, now, candidate.model_dump_json()),
            )
            return cursor.rowcount == 1

    def candidates(self, now: float) -> list[Candidate]:
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM events WHERE status='queued' ORDER BY created_at LIMIT 3").fetchall()
        return [Candidate.model_validate_json(row[0]) for row in rows if json.loads(row[0])["expires_at"] > now]

    def record(self, event_id: str, status: str, reason: str, now: float, review: Any = None) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE events SET status=?,reason=?,updated_at=?,review=COALESCE(?,review) WHERE event_id=?",
                (status, reason, now, encode(review) if review is not None else None, event_id),
            )

    def event(self, event_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            transport = db.execute("SELECT status,attempts,message_id,error_code,expires_at FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for key in ("payload", "review", "outcome"):
            value[key] = json.loads(value[key]) if value[key] else None
        value["notification"] = dict(transport) if transport else None
        return value

    def latest_events(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connection() as db:
            ids = [row[0] for row in db.execute("SELECT event_id FROM events ORDER BY created_at DESC,rowid DESC LIMIT ?", (limit,))]
        return [value for event_id in ids if (value := self.event(event_id)) is not None]

    def knowledge(self, strategy_ids: list[str], *, perpetual: bool) -> list[dict[str, Any]]:
        identifiers = sorted(set(strategy_ids))
        if not identifiers or any(value not in CARDS for value in identifiers):
            raise ValueError("unknown_strategy_card")
        expression = " OR ".join('"' + value + '"' for value in identifiers)
        placeholders = ",".join("?" for _ in identifiers)
        with self.connection() as db:
            rows = db.execute(
                "SELECT c.payload FROM cards c JOIN card_search s ON s.strategy_id=c.strategy_id "
                f"WHERE card_search MATCH ? AND c.strategy_id IN ({placeholders}) ORDER BY c.strategy_id",
                (expression, *identifiers),
            ).fetchall()
        if len(rows) != len(identifiers):
            raise ValueError("strategy_evidence_missing")
        cards = [json.loads(row[0]) for row in rows]
        source = "okx-public-data" if perpetual else "binance-market-data"
        return [
            *[{"evidence_id": f"card:{card['strategy_id']}", **card} for card in cards],
            {"evidence_id": source, **SOURCES[source], "content_hash": digest(SOURCES[source])},
        ]

    def review_budget(self, batch_id: str, now: float, payload: dict[str, Any]) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM review_batches WHERE batch_id=?", (batch_id,)).fetchone():
                db.rollback()
                return False
            recent = db.execute(
                "SELECT SUM(created_at>?),COUNT(*) FROM review_batches WHERE created_at>?",
                (now - 3600, now - 86400),
            ).fetchone()
            last = db.execute("SELECT MAX(created_at) FROM review_batches").fetchone()[0]
            probe_recent = payload.get("kind") == "authentication_probe" and db.execute(
                "SELECT 1 FROM review_batches WHERE created_at>? AND batch_id LIKE 'probe:%' LIMIT 1",
                (now - 3600,),
            ).fetchone() is not None
            if probe_recent or (recent[0] or 0) >= 4 or recent[1] >= 96 or (last is not None and now < last):
                db.rollback()
                return False
            db.execute("INSERT INTO review_batches VALUES (?,?,?)", (batch_id, now, encode(payload)))
            db.commit()
            return True

    def finish_review(self, batch_id: str, payload: dict[str, Any]) -> None:
        with self.connection() as db:
            db.execute("UPDATE review_batches SET payload=? WHERE batch_id=?", (encode(payload), batch_id))

    def recover(self, now: float) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE outbox SET status='unknown',error_code='interrupted_send' WHERE status='sending'")
            db.execute("UPDATE events SET status='review_unknown',reason='interrupted_review',updated_at=? WHERE status='reviewing'", (now,))
            db.execute("UPDATE outbox SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
            db.execute("UPDATE events SET status='expired',reason='candidate_expired',updated_at=? WHERE status IN ('queued','approved','cooldown') AND json_extract(payload,'$.expires_at')<=?", (now, now))
            db.commit()

    def enqueue(self, event_id: str, text: str, expires_at: float, now: float, *, is_test: bool = False) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO outbox(event_id,created_at,expires_at,status,payload,is_test) VALUES (?,?,?,'pending',?,?)",
                (event_id, now, expires_at, encode({"text": text}), int(is_test)),
            )

    def request_test(self, request_id: str, now: float, revision: str) -> str:
        event_id = "test-" + digest(request_id)[:24]
        text = (
            f"QT 测试｜BTC 提醒链路验收\n编号：{event_id}\nUnix 时间：{int(now)}\n"
            f"版本：{revision}\n这是通知传输测试；模型审核状态请查看 QT 页面。\n本消息用于功能测试，未执行交易。"
        )
        with self.connection() as db:
            if db.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= MAX_EVENTS:
                raise ValueError("alert_event_capacity")
            db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,'test_queued','transport_test',?,NULL,NULL)", (event_id, now, now, encode({"is_test": True, "expires_at": now + 600})))
        self.enqueue(event_id, text, now + 600, now, is_test=True)
        return event_id

    def pending(self, now: float) -> list[dict[str, Any]]:
        with self.connection() as db:
            db.execute("UPDATE outbox SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
            return [dict(row) | {"payload": json.loads(row["payload"])} for row in db.execute(
                "SELECT * FROM outbox WHERE status='pending' AND next_retry_at<=? AND expires_at>? ORDER BY created_at,rowid LIMIT 16", (now, now),
            )]

    def claim(self, event_id: str, recipient_key: str, now: float, *, strategy_version: str | None = None) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
            if item is None or item["status"] != "pending" or item["expires_at"] <= now or item["next_retry_at"] > now:
                db.rollback()
                return False
            policy = Policy.model_validate_json(db.execute("SELECT payload FROM policy").fetchone()[0])
            if (not item["is_test"] and (policy.mode != "notify" or strategy_version != policy.strategy_version)) or (item["is_test"] and policy.mode == "off"):
                db.rollback()
                return False
            quota = db.execute("SELECT * FROM quota WHERE recipient_key=?", (recipient_key,)).fetchone()
            if quota and (now < quota["next_allowed_at"] or now < quota["last_seen_at"]):
                db.rollback()
                return False
            previous = quota["next_allowed_at"] if quota else 0
            db.execute("INSERT OR REPLACE INTO quota VALUES (?,?,?,?,?)", (recipient_key, now + 3600, previous, now, event_id))
            db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE event_id=?", (event_id,))
            db.commit()
            return True

    def sent(self, event_id: str, recipient_key: str, now: float, status: str, *,
             message_id: int | None = None, error_code: str | None = None, retry_at: float = 0) -> None:
        if status not in {"accepted", "unknown", "failed", "pending", "expired", "obsolete"}:
            raise ValueError("invalid_send_status")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE outbox SET status=?,message_id=?,error_code=?,next_retry_at=? WHERE event_id=?", (status, message_id, error_code, retry_at, event_id))
            if status in {"accepted", "unknown"}:
                db.execute("UPDATE quota SET next_allowed_at=MAX(next_allowed_at,?),last_seen_at=MAX(last_seen_at,?) WHERE recipient_key=? AND owner=?", (now + 3600, now, recipient_key, event_id))
            else:
                db.execute("UPDATE quota SET next_allowed_at=previous_allowed_at,owner=NULL,last_seen_at=MAX(last_seen_at,?) WHERE recipient_key=? AND owner=?", (now, recipient_key, event_id))
            db.execute("UPDATE events SET status=?,reason=?,updated_at=? WHERE event_id=?", (status, error_code or status, now, event_id))
            db.commit()

    def next_allowed(self, recipient_key: str) -> float:
        with self.connection() as db:
            row = db.execute("SELECT next_allowed_at FROM quota WHERE recipient_key=?", (recipient_key,)).fetchone()
            return float(row[0]) if row else 0

    def validation(self, version: str, strategy_id: str, direction: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM validations WHERE strategy_version=? AND strategy_id=? AND direction=?", (version, strategy_id, direction)).fetchone()
        return json.loads(row[0]) if row else {"status": "unvalidated", "oos_events": 0}

    def save_validation(self, version: str, strategy_id: str, direction: str, now: float, result: dict[str, Any]) -> None:
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO validations VALUES (?,?,?,?,?)", (version, strategy_id, direction, now, encode(result)))

    def promotion_reasons(self, candidate: Candidate, now: float) -> list[str]:
        reasons = []
        for trigger in candidate.triggers:
            value = self.validation(candidate.strategy_version, trigger.strategy_id, candidate.direction)
            if (
                value.get("status") != "passed" or value.get("oos_events", 0) < 100
                or value.get("ci95_lower", 0) <= 0
                or not value.get("costs_included") or not value.get("purged")
                or (candidate.direction == "short" and not value.get("funding_complete"))
            ):
                reasons.append(f"validation_pending:{trigger.strategy_id}:{candidate.direction}")
        started = self.get(f"shadow_started:{candidate.strategy_version}", now)
        with self.connection() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM events WHERE json_extract(outcome,'$.status')='labelled' AND json_extract(payload,'$.strategy_version')=?",
                (candidate.strategy_version,),
            ).fetchone()[0]
        if now - started < 7 * 86400 or count < 20:
            reasons.append("forward_shadow_evidence_pending")
        return reasons

    def expire_and_retain(self, now: float) -> None:
        with self.connection() as db:
            db.execute("UPDATE outbox SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
            db.execute("UPDATE events SET status='expired',reason='candidate_expired',updated_at=? WHERE status IN ('queued','approved','cooldown','test_queued') AND json_extract(payload,'$.expires_at')<=?", (now, now))
            db.execute("DELETE FROM review_batches WHERE created_at<?", (now - 90 * 86400,))
            # Keep unresolved transport outcomes and the independent quota ledger.
            db.execute("DELETE FROM outbox WHERE created_at<? AND status NOT IN ('pending','sending','unknown')", (now - 90 * 86400,))
            db.execute("DELETE FROM events WHERE created_at<? AND event_id NOT IN (SELECT event_id FROM outbox) AND outcome IS NOT NULL", (now - 90 * 86400,))

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source, sqlite3.connect(destination) as target:
            source.backup(target)
        os.chmod(destination, 0o600)
