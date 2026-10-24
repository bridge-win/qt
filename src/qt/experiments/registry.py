"""Immutable campaigns and trials; one audited holdout grant per owned window.

This repository uses the central PostgreSQL metadata and migrations. SQLite is
supported only by the explicit offline/test engine. Permission sets are trusted
service authentication context, never request/model-supplied JSON.
"""

from __future__ import annotations

import time
import uuid
from contextlib import nullcontext
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    Integer,
    String,
    Table,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection

from qt.persistence.repository import Conflict, Repository, checksum, insert_once
from qt.persistence.schema import jobs, metadata

dataset_guards = Table(
    "campaign_dataset_guards", metadata,
    Column("tenant_id", String(80), primary_key=True),
    Column("dataset_id", String(160), primary_key=True),
)
campaigns = Table(
    "experiment_campaigns", metadata,
    Column("tenant_id", String(80), primary_key=True),
    Column("campaign_id", String(160), primary_key=True),
    Column("dataset_id", String(160), nullable=False),
    Column("holdout_start_ns", BigInteger, nullable=False),
    Column("holdout_end_ns", BigInteger, nullable=False),
    Column("candidate_limit", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("created_ns", BigInteger, nullable=False),
    CheckConstraint("holdout_end_ns > holdout_start_ns AND candidate_limit > 0", name="ck_campaign_bounds"),
)
trials = Table(
    "experiment_trials", metadata,
    Column("tenant_id", String(80), primary_key=True),
    Column("trial_id", String(160), primary_key=True),
    Column("campaign_id", String(160), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("result", JSON),
    Column("result_sha256", String(64)),
    Column("created_ns", BigInteger, nullable=False),
)
holdout_grants = Table(
    "campaign_holdout_grants", metadata,
    Column("tenant_id", String(80), primary_key=True),
    Column("campaign_id", String(160), primary_key=True),
    Column("grant_id", String(64), nullable=False, unique=True),
    Column("actor_id", String(160), nullable=False),
    Column("reason", String(1000), nullable=False),
    Column("granted_ns", BigInteger, nullable=False),
)


class ExperimentRegistry(Repository):
    """Central campaign accounting; constructing it does not migrate schemas."""

    def register_campaign(
        self, tenant_id: str, campaign_id: str, *, dataset_id: str,
        holdout_start_ns: int, holdout_end_ns: int,
        protocol: dict[str, Any], candidate_limit: int = 30,
    ) -> dict[str, Any]:
        if not tenant_id or not campaign_id or not dataset_id:
            raise ValueError("campaign_identity_required")
        if not 0 <= holdout_start_ns < holdout_end_ns or not 1 <= candidate_limit <= 30:
            raise ValueError("invalid_campaign_bounds")
        payload = {
            "dataset_id": dataset_id, "holdout_start_ns": holdout_start_ns,
            "holdout_end_ns": holdout_end_ns, "protocol": protocol,
            "candidate_limit": candidate_limit,
        }
        digest = checksum(payload)
        with self.transaction() as db:
            insert_once(db, dataset_guards, {"tenant_id": tenant_id, "dataset_id": dataset_id})
            db.execute(select(dataset_guards).where(
                dataset_guards.c.tenant_id == tenant_id, dataset_guards.c.dataset_id == dataset_id,
            ).with_for_update()).one()
            existing = db.execute(select(campaigns).where(
                campaigns.c.tenant_id == tenant_id, campaigns.c.campaign_id == campaign_id,
            )).mappings().first()
            if existing is not None:
                if existing["sha256"] != digest:
                    raise Conflict("campaign_identity_conflict")
                return dict(existing)
            overlap = db.execute(select(campaigns.c.campaign_id).where(
                campaigns.c.tenant_id == tenant_id, campaigns.c.dataset_id == dataset_id,
                campaigns.c.holdout_start_ns < holdout_end_ns,
                campaigns.c.holdout_end_ns > holdout_start_ns,
            )).first()
            if overlap is not None:
                raise Conflict("holdout_window_already_owned_by_campaign")
            row = {
                "tenant_id": tenant_id, "campaign_id": campaign_id, "dataset_id": dataset_id,
                "holdout_start_ns": holdout_start_ns, "holdout_end_ns": holdout_end_ns,
                "candidate_limit": candidate_limit, "payload": payload, "sha256": digest,
                "created_ns": time.time_ns(),
            }
            db.execute(insert(campaigns).values(**row))
            self.event(db, tenant_id, campaign_id, "campaign_registered", {"sha256": digest})
            return row

    def campaign(self, tenant_id: str, campaign_id: str) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute(select(campaigns).where(
                campaigns.c.tenant_id == tenant_id, campaigns.c.campaign_id == campaign_id,
            )).mappings().first()
        if row is None:
            raise KeyError("campaign_not_found")
        return dict(row)

    def register_trial(
        self, tenant_id: str, campaign_id: str, trial_id: str, proposal: dict[str, Any],
        *, fence_job: tuple[str, str] | None = None, connection: Connection | None = None,
    ) -> dict[str, Any]:
        if not trial_id or not proposal:
            raise ValueError("trial_identity_and_proposal_required")
        digest = checksum({"campaign_id": campaign_id, "proposal": proposal})
        with nullcontext(connection) if connection is not None else self.transaction() as db:
            if fence_job is not None:
                self.assert_job_fence(db, tenant_id, fence_job)
            campaign = db.execute(select(campaigns).where(
                campaigns.c.tenant_id == tenant_id, campaigns.c.campaign_id == campaign_id,
            ).with_for_update()).mappings().first()
            if campaign is None:
                raise KeyError("campaign_not_found")
            existing = db.execute(select(trials).where(
                trials.c.tenant_id == tenant_id, trials.c.trial_id == trial_id,
            )).mappings().first()
            if existing is not None:
                if existing["sha256"] != digest:
                    raise Conflict("trial_identity_conflict")
                return dict(existing)
            opened = db.execute(select(holdout_grants.c.grant_id).where(
                holdout_grants.c.tenant_id == tenant_id, holdout_grants.c.campaign_id == campaign_id,
            )).first()
            if opened:
                raise Conflict("campaign_frozen_after_holdout_unlock")
            count = db.execute(select(func.count()).select_from(trials).where(
                trials.c.tenant_id == tenant_id, trials.c.campaign_id == campaign_id,
            )).scalar_one()
            if count >= campaign["candidate_limit"]:
                raise Conflict("campaign_candidate_limit_exceeded")
            row = {
                "tenant_id": tenant_id, "trial_id": trial_id, "campaign_id": campaign_id,
                "payload": proposal, "sha256": digest, "status": "submitted", "result": None,
                "result_sha256": None, "created_ns": time.time_ns(),
            }
            db.execute(insert(trials).values(**row))
            self.event(db, tenant_id, trial_id, "trial_registered", {"campaign_id": campaign_id, "sha256": digest})
            return row

    def finish_trial(
        self, tenant_id: str, trial_id: str, *, status: str, result: dict[str, Any],
        fence_job: tuple[str, str] | None = None, connection: Connection | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "aborted", "rejected"}:
            raise ValueError("invalid_terminal_trial_status")
        digest = checksum({"status": status, "result": result})
        with nullcontext(connection) if connection is not None else self.transaction() as db:
            if fence_job is not None:
                self.assert_job_fence(db, tenant_id, fence_job)
            row = db.execute(select(trials).where(
                trials.c.tenant_id == tenant_id, trials.c.trial_id == trial_id,
            ).with_for_update()).mappings().first()
            if row is None:
                raise KeyError("trial_not_found")
            if row["status"] != "submitted":
                if row["result_sha256"] != digest:
                    raise Conflict("terminal_trial_result_immutable")
                return dict(row)
            db.execute(update(trials).where(
                trials.c.tenant_id == tenant_id, trials.c.trial_id == trial_id,
            ).values(status=status, result=result, result_sha256=digest))
            self.event(db, tenant_id, trial_id, "trial_finished", {"status": status, "sha256": digest})
            return {**dict(row), "status": status, "result": result, "result_sha256": digest}

    def trial_population(self, tenant_id: str, campaign_id: str) -> list[dict[str, Any]]:
        self.campaign(tenant_id, campaign_id)
        with self.transaction() as db:
            rows = db.execute(select(trials).where(
                trials.c.tenant_id == tenant_id, trials.c.campaign_id == campaign_id,
            ).order_by(trials.c.created_ns, trials.c.trial_id)).mappings().all()
        return [dict(row) for row in rows]

    def reconcile_terminal_jobs(self, tenant_id: str, *, limit: int = 100) -> int:
        """Finish abandoned trials from durable queue facts, never a stale worker.

        Cancel-before-claim, deadline expiry and exhausted crash retries must
        remain counted in the search population without staying 'submitted'
        forever. A still-active legacy duplicate job takes precedence.
        """
        if not 1 <= limit <= 100:
            raise ValueError("terminal_trial_reconciliation_limit_invalid")
        other = jobs.alias("other_attempt")
        active = select(other.c.job_id).where(
            other.c.tenant_id == jobs.c.tenant_id, other.c.kind == "experiment",
            other.c.sha256 == jobs.c.sha256, other.c.status.in_(("QUEUED", "RUNNING")),
        ).exists()
        count = 0
        with self.transaction() as connection:
            rows = connection.execute(select(jobs).join(trials,
                (trials.c.tenant_id == jobs.c.tenant_id)
                & (trials.c.trial_id == jobs.c.payload["experiment_hash"].as_string()),
            ).where(jobs.c.tenant_id == tenant_id, jobs.c.kind == "experiment",
                    jobs.c.status.in_(("CANCELLED", "EXHAUSTED", "FAILED")),
                    trials.c.status == "submitted", ~active)
                .order_by(jobs.c.created_ns, jobs.c.job_id).limit(limit)
                .with_for_update(of=jobs, skip_locked=True)).mappings().all()
            for job in rows:
                if checksum(job["payload"]) != job["sha256"]:
                    continue
                current = connection.execute(select(trials.c.status).where(
                    trials.c.tenant_id == tenant_id,
                    trials.c.trial_id == job["payload"]["experiment_hash"],
                ).with_for_update()).scalar_one()
                if current != "submitted":
                    continue
                self.finish_trial(tenant_id, job["payload"]["experiment_hash"],
                                  status="failed" if job["status"] == "FAILED" else "aborted",
                                  result={"job_id": job["job_id"], "queue_status": job["status"],
                                          "attempts": job["attempts"], "queue_result_hash": checksum(job["result"])},
                                  connection=connection)
                count += 1
        return count

    def unlock_holdout(
        self, tenant_id: str, campaign_id: str, *, actor_id: str,
        permissions: frozenset[str], reason: str,
    ) -> dict[str, Any]:
        """Issue one formal grant; register all candidates BEFORE calling this."""
        error: str | None = None
        grant: dict[str, Any] | None = None
        with self.transaction() as db:
            campaign = db.execute(select(campaigns).where(
                campaigns.c.tenant_id == tenant_id, campaigns.c.campaign_id == campaign_id,
            ).with_for_update()).mappings().first()
            if campaign is None:
                raise KeyError("campaign_not_found")
            if "holdout:unlock" not in permissions or not actor_id or not reason.strip():
                error = "holdout_unlock_permission_or_reason_required"
            elif db.execute(select(holdout_grants).where(
                holdout_grants.c.tenant_id == tenant_id, holdout_grants.c.campaign_id == campaign_id,
            )).first():
                error = "holdout_already_unlocked"
            else:
                population = db.execute(select(trials.c.status).where(
                    trials.c.tenant_id == tenant_id, trials.c.campaign_id == campaign_id,
                )).scalars().all()
                if not population or any(status == "submitted" for status in population):
                    error = "campaign_trials_not_terminal"
            if error is None:
                grant = {
                    "tenant_id": tenant_id, "campaign_id": campaign_id,
                    "grant_id": uuid.uuid4().hex, "actor_id": actor_id,
                    "reason": reason, "granted_ns": time.time_ns(),
                }
                db.execute(insert(holdout_grants).values(**grant))
            self.event(db, tenant_id, campaign_id, "holdout_unlock_denied" if error else "holdout_unlocked", {
                "actor_id": actor_id, "reason": reason, "denial": error,
                "grant_id": grant["grant_id"] if grant else None,
            })
        if error:
            raise PermissionError(error)
        assert grant is not None
        return grant

    def authorize_holdout_read(
        self, tenant_id: str, campaign_id: str, *, actor_id: str,
        grant_id: str, permissions: frozenset[str],
    ) -> dict[str, Any]:
        """Data readers must call this; a proposer has no read capability."""
        with self.transaction() as db:
            row = db.execute(select(holdout_grants).where(
                holdout_grants.c.tenant_id == tenant_id,
                holdout_grants.c.campaign_id == campaign_id,
                holdout_grants.c.actor_id == actor_id,
                holdout_grants.c.grant_id == grant_id,
            )).mappings().first()
            allowed = row is not None and "holdout:read" in permissions
            self.event(db, tenant_id, campaign_id, "holdout_read" if allowed else "holdout_read_denied", {"actor_id": actor_id})
        if not allowed:
            raise PermissionError("holdout_read_not_authorized")
        return self.campaign(tenant_id, campaign_id)

    def guard_development_read(
        self, tenant_id: str, *, dataset_id: str, start_ns: int, end_ns: int
    ) -> None:
        if not 0 <= start_ns < end_ns:
            raise ValueError("invalid_read_window")
        with self.transaction() as db:
            protected = db.execute(select(campaigns.c.campaign_id).where(
                campaigns.c.tenant_id == tenant_id, campaigns.c.dataset_id == dataset_id,
                campaigns.c.holdout_start_ns < end_ns, campaigns.c.holdout_end_ns > start_ns,
            )).first()
        if protected is not None:
            raise PermissionError("development_read_overlaps_protected_holdout")
