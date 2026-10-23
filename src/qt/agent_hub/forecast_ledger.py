"""Immutable forecasts and append-only resolution evidence.

Scores describe forecasts, never execution or trading P&L. Repeated forecasts
are selected by an explicit, deterministic cohort policy, and missing market
probabilities are never replaced with an invented baseline.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from qt.agent_hub.research import ResolvedForecast, evaluate_forecasts

_IDENTITY_COLUMNS = {
    "event_id": "TEXT",
    "event_family_id": "TEXT",
    "outcome_id": "TEXT",
    "rule_version": "TEXT",
    "model_version": "TEXT",
}
_RESOLUTION_FIELDS = (
    "forecast_id", "status", "outcome", "settle_ts", "settle_source", "payout",
    "redeemed_ts", "dispute",
)


def _number(
    value: Any, name: str, *, minimum: float = 0, maximum: float | None = None,
    positive: bool = False,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}_must_be_a_finite_number")
    number = float(value)
    if (
        not math.isfinite(number) or number < minimum
        or (positive and number <= 0) or (maximum is not None and number > maximum)
    ):
        raise ValueError(f"{name}_out_of_range")
    return number


def _identity(value: Any, name: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError(f"{name}_must_be_a_nonempty_string")
    return value


def _alias(payload: dict[str, Any], first: str, second: str) -> Any:
    """A zero probability is a value; conflicting aliases are an invalid request."""
    primary, alias = payload.get(first), payload.get(second)
    if primary is not None and alias is not None and primary != alias:
        raise ValueError(f"conflicting_{first}_aliases")
    return primary if primary is not None else alias


class ForecastLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS forecasts (
                    forecast_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    run_id TEXT, market_id TEXT NOT NULL, token_id TEXT,
                    round_start_unix INTEGER, p_model REAL, p_market_mid REAL,
                    best_bid REAL, best_ask REAL, fee_at_p REAL, horizon_s INTEGER,
                    decision_time REAL NOT NULL, knowledge_time REAL,
                    evidence_receipt TEXT, net_edge_reason TEXT, created_at REAL NOT NULL
                )"""
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(forecasts)")}
            for name, kind in _IDENTITY_COLUMNS.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE forecasts ADD COLUMN {name} {kind}")
            db.execute(
                """CREATE TABLE IF NOT EXISTS resolution_events (
                    resolution_id TEXT PRIMARY KEY,
                    forecast_id TEXT NOT NULL REFERENCES forecasts(forecast_id),
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    outcome INTEGER, settle_ts REAL, settle_source TEXT, payout REAL,
                    redeemed_ts REAL, dispute INTEGER NOT NULL DEFAULT 0,
                    recorded_at REAL NOT NULL,
                    UNIQUE(forecast_id, revision)
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS forecasts_tenant_event "
                "ON forecasts(tenant_id, market_id, decision_time)"
            )
            self._migrate_resolutions(db)
            for table in ("resolution_events", "resolutions_legacy", "forecasts"):
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    for operation in ("UPDATE", "DELETE"):
                        db.execute(
                            f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                            f"BEFORE {operation} ON {table} BEGIN "
                            f"SELECT RAISE(ABORT, '{table}_is_immutable'); END"
                        )

    @staticmethod
    def _migrate_resolutions(db: sqlite3.Connection) -> None:
        """Preserve every legacy row and expose the latest revision through a view.

        The former replacement table contains only its last value. Earlier values
        already overwritten by the old application cannot be reconstructed.
        """
        old = db.execute(
            "SELECT type FROM sqlite_master WHERE name='resolutions'"
        ).fetchone()
        if old is not None and old["type"] == "table":
            db.execute("ALTER TABLE resolutions RENAME TO resolutions_legacy")
            for row in db.execute("SELECT * FROM resolutions_legacy").fetchall():
                status = "disputed" if row["dispute"] else "resolved"
                if row["outcome"] is None and row["payout"] is None and not row["dispute"]:
                    status = "pending"
                db.execute(
                    "INSERT INTO resolution_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "legacy:" + row["forecast_id"], row["forecast_id"], 1,
                        status, row["outcome"], row["settle_ts"], row["settle_source"],
                        row["payout"], row["redeemed_ts"], row["dispute"], time.time(),
                    ),
                )
        db.execute(
            """CREATE VIEW IF NOT EXISTS resolutions AS
                SELECT r.forecast_id, r.outcome, r.settle_ts, r.settle_source,
                       r.payout, r.redeemed_ts, r.dispute
                FROM resolution_events r
                WHERE r.revision = (
                    SELECT MAX(latest.revision) FROM resolution_events latest
                    WHERE latest.forecast_id = r.forecast_id
                )"""
        )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def record_forecast(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        _identity(tenant_id, "tenant_id", required=True)
        forecast_id = _identity(payload.get("forecast_id", uuid.uuid4().hex), "forecast_id", required=True)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM forecasts WHERE forecast_id=?", (forecast_id,)
            ).fetchone()
            if existing is not None and existing["tenant_id"] != tenant_id:
                raise ValueError("forecast_id_conflict")
            now = time.time()
            decision = payload.get("decision_time")
            if decision is None:
                decision = existing["decision_time"] if existing is not None else now
            decision = _number(decision, "decision_time", positive=True, maximum=now)
            assert decision is not None
            knowledge = _number(payload.get("knowledge_time"), "knowledge_time", positive=True)
            if knowledge is not None and knowledge > decision:
                raise ValueError("knowledge_time_after_decision")
            bid = _number(payload.get("best_bid"), "best_bid", maximum=1)
            ask = _number(payload.get("best_ask"), "best_ask", maximum=1)
            if bid is not None and ask is not None and bid > ask:
                raise ValueError("crossed_probability_quote")
            horizon = _number(
                _alias(payload, "horizon_s", "horizon_seconds"), "horizon_s", positive=True,
            )
            round_start = payload.get("round_start_unix")
            if round_start is not None and (
                isinstance(round_start, bool) or not isinstance(round_start, int)
                or round_start < 0 or round_start > decision
            ):
                raise ValueError("invalid_round_start_unix")
            row = {
                "forecast_id": forecast_id,
                "tenant_id": tenant_id,
                "run_id": _identity(payload.get("run_id"), "run_id"),
                "market_id": _identity(
                    _alias(payload, "market_id", "instrument") or "BTCUSDT",
                    "market_id", required=True,
                ),
                "token_id": _identity(_alias(payload, "token_id", "outcome_token"), "token_id"),
                "round_start_unix": round_start,
                "p_model": _number(_alias(payload, "p_model", "probability"), "p_model", maximum=1),
                "p_market_mid": _number(payload.get("p_market_mid"), "p_market_mid", maximum=1),
                "best_bid": bid,
                "best_ask": ask,
                "fee_at_p": _number(payload.get("fee_at_p"), "fee_at_p", maximum=1),
                "horizon_s": horizon,
                "decision_time": decision,
                "knowledge_time": knowledge,
                "evidence_receipt": _identity(payload.get("evidence_receipt"), "evidence_receipt"),
                "net_edge_reason": payload.get("net_edge_reason")
                or "missing_independent_calibrated_probability",
                "created_at": existing["created_at"] if existing is not None else now,
                **{name: _identity(payload.get(name), name) for name in _IDENTITY_COLUMNS},
            }
            if existing is not None:
                if row != dict(existing):
                    raise ValueError("forecast_id_conflict")
                return dict(existing)
            db.execute(
                f"INSERT INTO forecasts ({','.join(row)}) VALUES({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
        return row

    @staticmethod
    def _resolution(
        forecast: sqlite3.Row, payload: dict[str, Any], previous: sqlite3.Row | None,
    ) -> dict[str, Any]:
        outcome = payload.get("outcome")
        if outcome is not None and (type(outcome) is not int or outcome not in (0, 1)):
            raise ValueError("outcome_must_be_binary_or_null")
        dispute = payload.get("dispute", False)
        if type(dispute) is not bool:
            raise ValueError("dispute_must_be_boolean")
        payout = _number(payload.get("payout"), "payout", maximum=1)
        status = payload.get("status") or (
            "disputed" if dispute else "resolved" if outcome is not None or payout is not None else "pending"
        )
        if status not in {"pending", "resolved", "canceled", "disputed"}:
            raise ValueError("invalid_resolution_status")
        if dispute and status != "disputed":
            raise ValueError("resolution_status_dispute_conflict")
        if status == "resolved" and outcome is None and payout is None:
            raise ValueError("resolution_requires_outcome_or_payout")
        if outcome is not None and payout is not None and payout != outcome:
            raise ValueError("nonbinary_payout_requires_null_outcome")
        now = time.time()
        settle_ts = payload.get("settle_ts")
        if settle_ts is None and status in {"resolved", "disputed"}:
            settle_ts = previous["settle_ts"] if previous is not None and previous["settle_ts"] else now
        settle_ts = _number(settle_ts, "settle_ts", positive=True, maximum=now)
        if settle_ts is not None and settle_ts <= forecast["decision_time"]:
            raise ValueError("resolution_must_follow_forecast")
        redeemed = _number(payload.get("redeemed_ts"), "redeemed_ts", positive=True, maximum=now)
        if redeemed is not None and (settle_ts is None or redeemed < settle_ts):
            raise ValueError("redemption_must_follow_settlement")
        return {
            "forecast_id": forecast["forecast_id"],
            "status": status,
            "outcome": outcome,
            "settle_ts": settle_ts,
            "settle_source": _identity(payload.get("settle_source", "unspecified"), "settle_source", required=True),
            "payout": payout,
            "redeemed_ts": redeemed,
            "dispute": int(status == "disputed"),
        }

    def resolve(self, tenant_id: str, forecast_id: str, resolution: dict[str, Any]) -> dict[str, Any]:
        """Append a revision; explicit resolution IDs provide retry idempotency."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            forecast = db.execute(
                "SELECT * FROM forecasts WHERE tenant_id=? AND forecast_id=?",
                (tenant_id, forecast_id),
            ).fetchone()
            if forecast is None:
                raise KeyError("forecast_not_found")
            previous = db.execute(
                "SELECT * FROM resolution_events WHERE forecast_id=? ORDER BY revision DESC LIMIT 1",
                (forecast_id,),
            ).fetchone()
            resolution_id = resolution.get("resolution_id")
            existing = None
            if resolution_id is not None:
                _identity(resolution_id, "resolution_id", required=True)
                existing = db.execute(
                    "SELECT * FROM resolution_events WHERE resolution_id=?", (resolution_id,)
                ).fetchone()
            body = self._resolution(forecast, resolution, existing if existing is not None else previous)
            if existing is not None:
                if body != {name: existing[name] for name in _RESOLUTION_FIELDS}:
                    raise ValueError("resolution_id_conflict")
                return dict(existing)
            if resolution_id is None and previous is not None and body == {
                name: previous[name] for name in _RESOLUTION_FIELDS
            }:
                return dict(previous)
            row = {
                "resolution_id": resolution_id or uuid.uuid4().hex,
                "forecast_id": forecast_id,
                "revision": previous["revision"] + 1 if previous is not None else 1,
                **{name: body[name] for name in _RESOLUTION_FIELDS if name != "forecast_id"},
                "recorded_at": time.time(),
            }
            db.execute(
                f"INSERT INTO resolution_events ({','.join(row)}) VALUES({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
        return row

    def resolution_history(self, tenant_id: str, forecast_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT r.* FROM resolution_events r JOIN forecasts f USING(forecast_id) "
                "WHERE f.tenant_id=? AND f.forecast_id=? ORDER BY r.revision",
                (tenant_id, forecast_id),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _event_key(row: sqlite3.Row) -> str:
        return json.dumps(
            [row["event_family_id"] or row["event_id"] or row["market_id"], row["round_start_unix"]],
            separators=(",", ":"),
        )

    @staticmethod
    def _cohort_key(row: sqlite3.Row) -> str:
        return json.dumps(
            [row["event_id"] or row["market_id"], row["round_start_unix"],
             row["outcome_id"] or row["token_id"], row["horizon_s"]],
            separators=(",", ":"),
        )

    def score(self, tenant_id: str) -> dict[str, Any]:
        with self._db() as db:
            rows = db.execute(
                "SELECT f.*, r.status AS resolution_status, r.outcome, r.payout, "
                "r.settle_ts, r.dispute FROM forecasts f "
                "LEFT JOIN resolution_events r ON r.forecast_id=f.forecast_id AND "
                "r.revision=(SELECT MAX(latest.revision) FROM resolution_events latest "
                "WHERE latest.forecast_id=f.forecast_id) "
                "WHERE f.tenant_id=? ORDER BY f.decision_time, f.forecast_id",
                (tenant_id,),
            ).fetchall()
        counts = {
            "total_forecasts": len(rows), "unresolved_forecasts": 0,
            "canceled_forecasts": 0, "disputed_forecasts": 0,
            "non_binary_resolutions": 0, "missing_model_probability": 0,
            "invalid_observations": 0, "duplicate_cohort_forecasts": 0,
        }
        cohorts: dict[str, sqlite3.Row] = {}
        for row in rows:
            status = row["resolution_status"]
            if status is None or status == "pending":
                counts["unresolved_forecasts"] += 1
            elif status == "canceled":
                counts["canceled_forecasts"] += 1
            elif status == "disputed" or row["dispute"]:
                counts["disputed_forecasts"] += 1
            elif row["outcome"] not in (0, 1) or (
                row["payout"] is not None and row["payout"] != row["outcome"]
            ):
                counts["non_binary_resolutions"] += 1
            if row["p_model"] is None:
                counts["missing_model_probability"] += 1
                continue
            # Select before looking at outcomes or baseline availability. Later,
            # better forecasts cannot replace an inconvenient first observation.
            key = self._cohort_key(row)
            if key in cohorts:
                counts["duplicate_cohort_forecasts"] += 1
            else:
                cohorts[key] = row
        resolved, event_keys = [], set()
        for key, row in cohorts.items():
            if (
                row["resolution_status"] != "resolved" or row["dispute"]
                or row["outcome"] not in (0, 1)
                or (row["payout"] is not None and row["payout"] != row["outcome"])
            ):
                continue
            try:
                resolved.append(
                    ResolvedForecast(
                        market_id=hashlib.sha256(key.encode()).hexdigest(),
                        prediction=row["p_model"], market_probability=row["p_market_mid"],
                        outcome=row["outcome"], predicted_at=row["decision_time"],
                        resolved_at=row["settle_ts"],
                    )
                )
                event_keys.add(self._event_key(row))
            except (TypeError, ValueError):
                # Historical rows remain auditable, but invalid legacy values
                # cannot crash scoring or be repaired with invented observations.
                counts["invalid_observations"] += 1
        report = evaluate_forecasts(resolved) if resolved else {
            "status": "empty", "samples": 0, "brier_score": None,
            "market_baseline_brier": None, "brier_skill": None, "log_loss": None,
            "calibration_bins": [], "live_ready": False,
            "paired_comparison_samples": 0, "comparison_coverage": 0.0,
            "market_baseline_coverage": 0.0, "paired_model_brier": None,
            "comparison_status": "missing_market_baseline",
            "limitations": [],
        }
        paired = sum(row.market_probability is not None for row in resolved)
        report.update(
            schema_version="forecast-scorecard.v2",
            counts=counts,
            scored_observations=len(resolved),
            independent_event_clusters=len(event_keys),
            effective_sample_size=None,
            paired_comparison_samples=paired,
            comparison_coverage=paired / len(resolved) if resolved else 0.0,
            selected_cohorts=len(cohorts),
            selection_policy="first_forecast_per_event_outcome_horizon",
            market_comparison_status="available" if paired else "missing_market_baseline",
            live_ready=False,
        )
        report["limitations"].extend([
            "Samples are selected event/outcome/horizon observations, not independent market trials",
            "Event families, common shocks and horizons can remain correlated; effective sample size is not inferred",
            "Missing event identities conservatively group the same market and round; explicit identities are required across related markets",
            "Special payouts, disputed, canceled and unresolved observations are excluded from binary scoring",
            "Resolution history predating the append-only migration cannot be reconstructed",
        ])
        return report

    def lookup(self, tenant_id: str, market_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM forecasts WHERE tenant_id=? AND market_id=? "
                "ORDER BY created_at DESC LIMIT 20",
                (tenant_id, market_id),
            ).fetchall()
        return [dict(row) for row in rows]


def net_edge_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    """Keep net edge unknown unless probability, quote and costs are supplied.

    Caller-supplied probabilities are not proof of calibration. Slippage and
    capital cost are per share, and supplied non-finite inputs are rejected.
    """
    probability = _number(candidate.get("estimated_probability"), "estimated_probability", maximum=1)
    ask = _number(candidate.get("best_ask"), "best_ask", maximum=1)
    fee = _number(candidate.get("fee_at_p"), "fee_at_p", maximum=1)
    slippage = _number(candidate.get("slippage_per_share", 0), "slippage_per_share")
    capital = _number(candidate.get("capital_cost_per_share", 0), "capital_cost_per_share")
    if probability is None or ask is None or fee is None or slippage is None or capital is None:
        return {
            "estimated_probability": probability, "net_edge": None,
            "net_edge_reason": "missing_independent_calibrated_probability_or_fee_schedule",
        }
    return {
        "estimated_probability": probability,
        "net_edge": probability - ask - fee - slippage - capital,
        "net_edge_reason": None,
    }
