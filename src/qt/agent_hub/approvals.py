"""Risk-tiered human approvals. Checkpoint is not an approval flow without TTL."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

RiskTier = Literal["read", "write", "funds", "irreversible"]


class ApprovalStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    effect_id TEXT,
                    risk_tier TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    decided_by TEXT,
                    decision TEXT,
                    signature TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS approvals_pending
                    ON approvals(tenant_id, decision, expires_at);
                """
            )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def request(
        self,
        tenant_id: str,
        run_id: str,
        payload: dict[str, Any],
        *,
        risk_tier: RiskTier = "funds",
        ttl_seconds: int = 3600,
        requested_by: str = "execute",
        effect_id: str | None = None,
    ) -> dict[str, Any]:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        item = {
            "id": uuid.uuid4().hex,
            "tenant_id": tenant_id,
            "run_id": run_id,
            "effect_id": effect_id,
            "risk_tier": risk_tier,
            "payload": json.loads(encoded),
            "requested_by": requested_by,
            "expires_at": time.time() + max(30, ttl_seconds),
            "decided_by": None,
            "decision": None,
            "signature": hashlib.sha256(encoded.encode()).hexdigest(),
            "created_at": time.time(),
        }
        with self._db() as db:
            db.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["id"],
                    tenant_id,
                    run_id,
                    effect_id,
                    risk_tier,
                    encoded,
                    requested_by,
                    item["expires_at"],
                    None,
                    None,
                    item["signature"],
                    item["created_at"],
                ),
            )
        return item

    def decide(
        self,
        tenant_id: str,
        approval_id: str,
        decision: Literal["once", "class", "reject"],
        *,
        decided_by: str = "owner",
    ) -> dict[str, Any]:
        now = time.time()
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND id=?",
                (tenant_id, approval_id),
            ).fetchone()
            if row is None:
                raise KeyError("approval_not_found")
            if row["decision"] is not None:
                raise ValueError("approval_already_decided")
            if row["expires_at"] <= now:
                raise ValueError("approval_expired_checkpoint")
            db.execute(
                "UPDATE approvals SET decision=?, decided_by=? WHERE tenant_id=? AND id=?",
                (decision, decided_by, tenant_id, approval_id),
            )
        return {**dict(row), "payload": json.loads(row["payload"]), "decision": decision, "decided_by": decided_by}

    def pending(self, tenant_id: str) -> list[dict[str, Any]]:
        now = time.time()
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND decision IS NULL "
                "ORDER BY created_at DESC LIMIT 100",
                (tenant_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(row["payload"])
            item["expired"] = row["expires_at"] <= now
            result.append(item)
        return result
