"""Durable forecasts and resolutions. Scoring is proper; it is not a trading P&L."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from qt.agent_hub.research import ResolvedForecast, evaluate_forecasts


class ForecastLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS forecasts (
                    forecast_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    run_id TEXT,
                    market_id TEXT NOT NULL,
                    token_id TEXT,
                    round_start_unix INTEGER,
                    p_model REAL,
                    p_market_mid REAL,
                    best_bid REAL,
                    best_ask REAL,
                    fee_at_p REAL,
                    horizon_s INTEGER,
                    decision_time REAL NOT NULL,
                    knowledge_time REAL,
                    evidence_receipt TEXT,
                    net_edge_reason TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resolutions (
                    forecast_id TEXT PRIMARY KEY,
                    outcome INTEGER,
                    settle_ts REAL,
                    settle_source TEXT,
                    payout REAL,
                    redeemed_ts REAL,
                    dispute INTEGER NOT NULL DEFAULT 0
                );
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

    def record_forecast(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        forecast_id = payload.get("forecast_id") or uuid.uuid4().hex
        row = {
            "forecast_id": forecast_id,
            "tenant_id": tenant_id,
            "run_id": payload.get("run_id"),
            "market_id": str(payload.get("market_id") or payload.get("instrument") or "BTCUSDT"),
            "token_id": payload.get("token_id") or payload.get("outcome_token"),
            "round_start_unix": payload.get("round_start_unix"),
            "p_model": payload.get("p_model") or payload.get("probability"),
            "p_market_mid": payload.get("p_market_mid"),
            "best_bid": payload.get("best_bid"),
            "best_ask": payload.get("best_ask"),
            "fee_at_p": payload.get("fee_at_p"),
            "horizon_s": payload.get("horizon_s") or payload.get("horizon_seconds"),
            "decision_time": float(payload.get("decision_time") or time.time()),
            "knowledge_time": payload.get("knowledge_time"),
            "evidence_receipt": payload.get("evidence_receipt"),
            "net_edge_reason": payload.get("net_edge_reason")
            or "missing_independent_calibrated_probability",
            "created_at": time.time(),
        }
        with self._db() as db:
            existing = db.execute(
                "SELECT * FROM forecasts WHERE forecast_id=?", (forecast_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            db.execute(
                "INSERT INTO forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row.values()),
            )
        return row

    def resolve(self, tenant_id: str, forecast_id: str, resolution: dict[str, Any]) -> dict[str, Any]:
        with self._db() as db:
            forecast = db.execute(
                "SELECT * FROM forecasts WHERE tenant_id=? AND forecast_id=?",
                (tenant_id, forecast_id),
            ).fetchone()
            if forecast is None:
                raise KeyError("forecast_not_found")
            db.execute(
                "INSERT OR REPLACE INTO resolutions VALUES(?,?,?,?,?,?,?)",
                (
                    forecast_id,
                    resolution.get("outcome"),
                    resolution.get("settle_ts") or time.time(),
                    resolution.get("settle_source") or "unspecified",
                    resolution.get("payout"),
                    resolution.get("redeemed_ts"),
                    1 if resolution.get("dispute") else 0,
                ),
            )
        return {"forecast_id": forecast_id, **resolution}

    def score(self, tenant_id: str) -> dict[str, Any]:
        with self._db() as db:
            rows = db.execute(
                "SELECT f.forecast_id, f.market_id, f.p_model, f.p_market_mid, r.outcome, "
                "f.decision_time, r.settle_ts FROM forecasts f "
                "JOIN resolutions r ON r.forecast_id=f.forecast_id "
                "WHERE f.tenant_id=? AND f.p_model IS NOT NULL AND r.outcome IN (0,1) "
                "AND r.dispute=0",
                (tenant_id,),
            ).fetchall()
        if not rows:
            return {"status": "empty", "samples": 0, "live_ready": False}
        resolved = [
            ResolvedForecast(
                market_id=row["market_id"] + ":" + row["forecast_id"],
                prediction=float(row["p_model"]),
                market_probability=float(row["p_market_mid"] if row["p_market_mid"] is not None else 0.5),
                outcome=int(row["outcome"]),
                predicted_at=float(row["decision_time"]),
                resolved_at=float(row["settle_ts"]),
            )
            for row in rows
        ]
        return evaluate_forecasts(resolved)

    def lookup(self, tenant_id: str, market_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM forecasts WHERE tenant_id=? AND market_id=? "
                "ORDER BY created_at DESC LIMIT 20",
                (tenant_id, market_id),
            ).fetchall()
        return [dict(row) for row in rows]


def net_edge_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    """Keep net_edge empty unless an independent calibrated probability exists."""
    probability = candidate.get("estimated_probability")
    ask = candidate.get("best_ask")
    fee = candidate.get("fee_at_p")
    if probability is None or ask is None or fee is None:
        return {
            "estimated_probability": probability,
            "net_edge": None,
            "net_edge_reason": "missing_independent_calibrated_probability_or_fee_schedule",
        }
    return {
        "estimated_probability": probability,
        "net_edge": float(probability) - float(ask) - float(fee),
        "net_edge_reason": None,
    }
