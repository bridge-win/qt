"""Append-only search accounting. DSR/PBO cannot detect leakage; this still records every trial."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class TrialLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS trial_ledger (
                    trial_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    strategy_spec_hash TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    cost_model_hash TEXT NOT NULL,
                    split_scheme TEXT NOT NULL,
                    arm TEXT NOT NULL,
                    is_sharpe REAL,
                    oos_sharpe REAL,
                    oos_return REAL,
                    oos_mdd REAL,
                    path_ref TEXT,
                    proposer TEXT,
                    holdout_sealed INTEGER NOT NULL DEFAULT 1,
                    holdout_unlocked_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS trial_snapshot
                    ON trial_ledger(tenant_id, snapshot_id, created_at);
                CREATE TABLE IF NOT EXISTS holdout_audit (
                    audit_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, trial_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL, reason TEXT NOT NULL,
                    action TEXT NOT NULL, created_at REAL NOT NULL
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(trial_ledger)")}
            if "registration_sha256" not in columns:
                db.execute("ALTER TABLE trial_ledger ADD COLUMN registration_sha256 TEXT")
            if "status" not in columns:
                db.execute("ALTER TABLE trial_ledger ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, tenant_id: str, trial: dict[str, Any]) -> dict[str, Any]:
        if trial.get("holdout_unlocked_at") is not None or trial.get("holdout_sealed", True) is not True:
            raise ValueError("holdout_unlock_requires_authorized_service")
        status = trial.get("status", "completed")
        if status not in {"submitted", "completed", "failed", "aborted", "rejected"}:
            raise ValueError("invalid_trial_status")
        trial_id = trial.get("trial_id") or uuid.uuid4().hex
        row = {
            "trial_id": trial_id,
            "tenant_id": tenant_id,
            "snapshot_id": str(trial.get("snapshot_id") or "unspecified"),
            "strategy_spec_hash": str(trial.get("strategy_spec_hash") or ""),
            "params_hash": str(trial.get("params_hash") or ""),
            "cost_model_hash": str(trial.get("cost_model_hash") or ""),
            "split_scheme": str(trial.get("split_scheme") or "walk_forward"),
            "arm": str(trial.get("arm") or "hypothesis"),
            "is_sharpe": trial.get("is_sharpe"),
            "oos_sharpe": trial.get("oos_sharpe"),
            "oos_return": trial.get("oos_return"),
            "oos_mdd": trial.get("oos_mdd"),
            "path_ref": trial.get("path_ref"),
            "proposer": trial.get("proposer") or "agent_hub",
            "holdout_sealed": 1 if trial.get("holdout_sealed", True) else 0,
            "holdout_unlocked_at": trial.get("holdout_unlocked_at"),
            "created_at": time.time(),
        }
        immutable = {key: value for key, value in row.items() if key not in {
            "created_at", "holdout_sealed", "holdout_unlocked_at",
        }}
        digest = hashlib.sha256(_json({**immutable, "status": status}).encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM trial_ledger WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if existing is not None:
                existing_immutable = {key: existing[key] for key in immutable}
                stored_digest = existing["registration_sha256"] or hashlib.sha256(
                    _json({**existing_immutable, "status": existing["status"]}).encode()
                ).hexdigest()
                if stored_digest != digest:
                    raise ValueError("trial_identity_payload_conflict")
                return {**dict(existing), "holdout_sealed": bool(existing["holdout_sealed"])}
            prior_unlock = db.execute(
                "SELECT holdout_unlocked_at FROM trial_ledger "
                "WHERE tenant_id=? AND snapshot_id=? AND holdout_unlocked_at IS NOT NULL LIMIT 1",
                (tenant_id, row["snapshot_id"]),
            ).fetchone()
            if prior_unlock is not None:
                raise ValueError("snapshot_frozen_after_holdout_unlock")
            row.update(registration_sha256=digest, status=status)
            placeholders = ",".join("?" for _ in row)
            db.execute(
                f"INSERT INTO trial_ledger ({','.join(row)}) VALUES({placeholders})",
                tuple(row.values()),
            )
        return {**row, "holdout_sealed": bool(row["holdout_sealed"])}

    def count(self, tenant_id: str, snapshot_id: str | None = None) -> int:
        with self._db() as db:
            if snapshot_id:
                row = db.execute(
                    "SELECT COUNT(*) FROM trial_ledger WHERE tenant_id=? AND snapshot_id=?",
                    (tenant_id, snapshot_id),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM trial_ledger WHERE tenant_id=?", (tenant_id,)
                ).fetchone()
        return int(row[0])

    def unlock_holdout(
        self, tenant_id: str, trial_id: str, *, actor_id: str = "",
        permissions: frozenset[str] = frozenset(), reason: str = "",
    ) -> dict[str, Any]:
        """Legacy snapshot-level unlock. V4 data uses ExperimentRegistry grants.

        Authenticated service permissions are required. A different trial ID
        cannot re-unlock or register more candidates on the exposed snapshot.
        """
        now = time.time()
        error = None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM trial_ledger WHERE tenant_id=? AND trial_id=?",
                (tenant_id, trial_id),
            ).fetchone()
            if existing is None:
                raise KeyError("trial_not_found")
            if "holdout:unlock" not in permissions or not actor_id or not reason.strip():
                error = "holdout_unlock_permission_or_reason_required"
            elif db.execute(
                "SELECT 1 FROM trial_ledger WHERE tenant_id=? AND snapshot_id=? "
                "AND holdout_unlocked_at IS NOT NULL LIMIT 1",
                (tenant_id, existing["snapshot_id"]),
            ).fetchone():
                error = "holdout_already_unlocked"
            if error is None:
                db.execute(
                    "UPDATE trial_ledger SET holdout_sealed=0, holdout_unlocked_at=? "
                    "WHERE tenant_id=? AND snapshot_id=?",
                    (now, tenant_id, existing["snapshot_id"]),
                )
            db.execute("INSERT INTO holdout_audit VALUES(?,?,?,?,?,?,?,?)", (
                uuid.uuid4().hex, tenant_id, existing["snapshot_id"], trial_id,
                actor_id, reason, error or "unlocked", now,
            ))
        if error is not None:
            raise PermissionError(error)
        return {"trial_id": trial_id, "holdout_unlocked_at": now, "once": True}

    def summary(self, tenant_id: str, snapshot_id: str, oos_sharpes: list[float]) -> dict[str, Any]:
        n_trials = self.count(tenant_id, snapshot_id)
        folds = len(oos_sharpes)
        mean = sum(oos_sharpes) / folds if folds else None
        with self._db() as db:
            unlocked = db.execute(
                "SELECT 1 FROM trial_ledger WHERE tenant_id=? AND snapshot_id=? "
                "AND holdout_unlocked_at IS NOT NULL LIMIT 1", (tenant_id, snapshot_id),
            ).fetchone()
            statuses = {row[0]: row[1] for row in db.execute(
                "SELECT status, COUNT(*) FROM trial_ledger WHERE tenant_id=? AND snapshot_id=? "
                "GROUP BY status", (tenant_id, snapshot_id),
            )}
        return {
            "n_trials": n_trials,
            "effective_trials": None,
            "walk_forward": {"folds": folds, "sharpe_mean": mean, "scheme": "chronological_oos_folds"},
            "cpcv": {
                "status": "not_applicable", "reason": "no_combinatorial_split_and_path_artifacts",
                "groups": None, "test_groups": None, "paths": 0,
                "sharpe_mean": None, "scheme": None,
            },
            "pbo": {
                "status": "not_applicable",
                "reason": "single hypothesis arm does not yield a CSCV selection matrix"
                if n_trials <= 1
                else "search arm recorded; CSCV requires the full trial matrix",
            },
            "dsr": {
                "status": "not_applicable", "value": None,
                "reason": "requires_complete_comparable_trial_sharpes_and_return_moments",
                "n_trials": n_trials,
                "effective_trials": None,
                "note": "DSR/PBO cannot detect look-ahead; trial ledger still records every candidate",
            },
            "holdout": {
                "sealed": unlocked is None,
                "policy": "legacy_metadata_only_v4_dataset_guard_required",
            },
            "search_accounting": {
                "submitted_candidates": n_trials,
                "terminal_status_counts": statuses,
                "global_selection_bias_corrected": False,
                "population_complete": False,
                "payload_sha256": None,
            },
            "contract": _json({"snapshot_id": snapshot_id, "n_trials": n_trials}),
        }
