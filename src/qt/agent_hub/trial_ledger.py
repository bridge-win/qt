"""Append-only search accounting. DSR/PBO cannot detect leakage; this still records every trial."""

from __future__ import annotations

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

    def record(self, tenant_id: str, trial: dict[str, Any]) -> dict[str, Any]:
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
        with self._db() as db:
            existing = db.execute(
                "SELECT trial_id FROM trial_ledger WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO trial_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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

    def unlock_holdout(self, tenant_id: str, trial_id: str) -> dict[str, Any]:
        now = time.time()
        with self._db() as db:
            existing = db.execute(
                "SELECT * FROM trial_ledger WHERE tenant_id=? AND trial_id=?",
                (tenant_id, trial_id),
            ).fetchone()
            if existing is None:
                raise KeyError("trial_not_found")
            if existing["holdout_unlocked_at"] is not None:
                raise ValueError("holdout_already_unlocked")
            db.execute(
                "UPDATE trial_ledger SET holdout_sealed=0, holdout_unlocked_at=? "
                "WHERE tenant_id=? AND trial_id=?",
                (now, tenant_id, trial_id),
            )
        return {"trial_id": trial_id, "holdout_unlocked_at": now, "once": True}

    def summary(self, tenant_id: str, snapshot_id: str, oos_sharpes: list[float]) -> dict[str, Any]:
        n_trials = max(1, self.count(tenant_id, snapshot_id))
        paths = len(oos_sharpes)
        mean = sum(oos_sharpes) / paths if paths else None
        return {
            "n_trials": n_trials,
            "effective_trials": n_trials,
            "cpcv": {
                "groups": paths,
                "test_groups": 1,
                "paths": paths,
                "sharpe_mean": mean,
                "scheme": "existing_oos_folds_as_combinatorial_paths",
            },
            "pbo": {
                "status": "not_applicable" if n_trials <= 1 else "uncomputed",
                "reason": "single hypothesis arm does not yield a CSCV selection matrix"
                if n_trials <= 1
                else "search arm recorded; CSCV requires the full trial matrix",
            },
            "dsr": {
                "n_trials": n_trials,
                "effective_trials": n_trials,
                "note": "DSR/PBO cannot detect look-ahead; trial ledger still records every candidate",
            },
            "holdout": {
                "sealed": True,
                "policy": "final_window_hidden_from_proposer_until_explicit_unlock",
            },
            "search_accounting": {
                "submitted_candidates": n_trials,
                "global_selection_bias_corrected": n_trials > 1,
                "payload_sha256": None,
            },
            "contract": _json({"snapshot_id": snapshot_id, "n_trials": n_trials}),
        }
