"""Fenced research and native experiment workers. No live order submission here."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from contextlib import suppress
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from qt.contracts import (
    ExperimentSpecV2,
    FeeSchedule,
    InstrumentSpec,
    StrategyArtifact,
    content_hash,
)
from qt.control.service import ControlService, parse
from qt.persistence import Conflict
from qt.persistence.schema import accounts, artifacts


def native_replay_hash(raw: dict) -> str:
    """Compare native semantics while retaining UUID provenance in raw reports.

    rc4 emits fresh event/initialization/position UUIDs on each run. Replace
    those UUIDs bijectively in first-seen order, preserving their relationships.
    Prices, quantities, fees, timestamps, client IDs, NAV and accounting still
    have to match exactly. The original UUIDs remain in the raw report.
    """
    identities: dict[str, str] = {}
    uuid_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

    def replace(match):
        identity = match.group()
        if identity not in identities:
            identities[identity] = f"native-uuid-{len(identities)}"
        return identities[identity]

    def stable(value, field=""):
        if isinstance(value, dict):
            return {key: stable(value[key], key) for key in sorted(value)
                    if not (value is raw and key == "sha256")}
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, str) and field in {"init_id", "event_id", "position_id"}:
            return re.sub(uuid_pattern, replace, value)
        return value
    return content_hash(stable(raw))


def causal_prefix(raw: dict, cutoff_ns: int) -> dict:
    """Economic observations before a cutoff; omit later order-state updates."""
    import pandas as pd

    def timestamp(value):
        return value if type(value) is int else int(pd.Timestamp(value).value)

    result = raw["result"]
    submissions = ("client_order_id", "trader_id", "strategy_id", "instrument_id", "side", "type",
                   "quantity", "time_in_force", "is_reduce_only", "is_quote_quantity", "ts_init", "tags")
    return {
        "nav": [row for row in result["nav"] if row["timestamp_ns"] < cutoff_ns],
        "decision_traces": [row for row in result["decision_traces"] if timestamp(row["timestamp"]) < cutoff_ns],
        "orders": [{key: row.get(key) for key in submissions} for row in result["orders"]
                   if timestamp(row["ts_init"]) < cutoff_ns],
        "fills": [row for row in result["fills"] if timestamp(row["ts_last"]) < cutoff_ns],
    }


class _PerturbedFuture:
    """Audit-only event view. Never published as a real data snapshot or P&L.

    The unmodified dataset remains verified; the audit records the precise
    synthetic mutation separately. Only future price/volume payloads change.
    """
    def __init__(self, datasets, cutoff):
        self.datasets, self.cutoff = datasets, cutoff

    def load(self, identity):
        return self.datasets.load(identity)

    def events(self, identity):
        from qt.contracts import MarketEvent

        for event in self.datasets.events(identity):
            if event.exchange_ts_ns > self.cutoff:
                payload = dict(event.payload)
                for key in ("open", "high", "low", "close", "volume"):
                    if key in payload:
                        payload[key] = str(Decimal(payload[key]) * 2)
                event = MarketEvent.model_validate({**event.model_dump(mode="python"), "payload": payload})
            yield event


def native_experiment(service: ControlService, job: dict, cancelled) -> dict:
    from qt.experiments.native import run

    repo, tenant = service.repository, job["tenant_id"]
    identity = job["payload"]["experiment_hash"]
    try:
        saved = repo.get(tenant, "experiment_result", identity)
    except KeyError:
        saved = None
    if saved is not None:
        if saved["payload"].get("experiment_hash") != identity:
            raise Conflict("cached_experiment_identity_mismatch")
        service.experiments.finish_trial(tenant, identity, status="completed", result={
            "result_hash": saved["sha256"], "gate_evidence": saved["payload"]["gate_evidence"],
        }, fence_job=(job["job_id"], job["lease_token"]))
        return {"experiment_hash": identity, "result_hash": saved["sha256"],
                "paper_eligible": service.validation(identity, "paper")["eligible"]}
    spec = parse(ExperimentSpecV2, repo.get(tenant, "experiment_spec", identity)["payload"])
    strategy = parse(StrategyArtifact, repo.get(tenant, "strategy", spec.strategy_version)["payload"])
    instruments = [parse(InstrumentSpec, repo.get(tenant, "instrument", version)["payload"])
                   for version in spec.instrument_spec_hashes]
    fees = [parse(FeeSchedule, repo.get(tenant, "fees", version)["payload"])
            for version in spec.cost_model.fee_schedule_hashes]
    folded = []
    duration = spec.split.test_end_ns - spec.split.validation_end_ns
    for fold in range(spec.split.folds):
        if cancelled():
            raise InterruptedError("experiment_cancelled")
        split = spec.split.model_dump(mode="json")
        for key in ("training_end_ns", "validation_end_ns", "test_end_ns"):
            split[key] += fold * duration
        split["folds"] = 1
        contract = parse(ExperimentSpecV2, {**spec.model_dump(mode="json"), "split": split})
        for dataset_id in contract.dataset_ids:
            service.experiments.guard_development_read(
                tenant, dataset_id=dataset_id, start_ns=contract.split.training_start_ns,
                end_ns=contract.split.test_end_ns + 1,
            )
        raw = run(contract, strategy, service.datasets, instruments, fees, cancelled=cancelled)
        replay = run(contract, strategy, service.datasets, instruments, fees, cancelled=cancelled)
        reproducible = native_replay_hash(raw) == native_replay_hash(replay)
        if not reproducible:
            raise ValueError("native_replay_not_reproducible")
        # A real truncated-prefix execution, not just matching declared window
        # fields. Exclude terminal observation: finalization can differ there.
        interval = contract.timeframes_ns[0]
        half_bars = (contract.split.test_end_ns - contract.split.validation_end_ns) // interval // 2
        prefix_equal = False
        mutation_equal = False
        if half_bars >= 3:
            end = contract.split.validation_end_ns + half_bars * interval
            prefix_spec = parse(ExperimentSpecV2, {
                **contract.model_dump(mode="json"),
                "split": {**split, "test_end_ns": end},
            })
            prefix = run(prefix_spec, strategy, service.datasets, instruments, fees, cancelled=cancelled)
            boundary = end + raw["actual_receipt_delay_ns"] + contract.latency_model.feed_ns
            original = causal_prefix(raw, boundary)
            prefix_equal = bool(original["nav"]) and (
                native_replay_hash(causal_prefix(prefix, boundary)) == native_replay_hash(original))
            poisoned = run(contract, strategy, _PerturbedFuture(service.datasets, end),
                           instruments, fees, cancelled=cancelled)
            mutation_equal = native_replay_hash(causal_prefix(poisoned, boundary)) == native_replay_hash(original)
        folded.append({"fold": fold + 1, "spec": contract.model_dump(mode="json"),
                       "raw": raw, "prefix_invariant": prefix_equal,
                       "future_mutation_invariant": mutation_equal,
                       "causality_scope": "registered_builtin_nav_signals_order_submissions_and_fills_before_midpoint",
                       "reproducible": reproducible,
                       "replay_semantic_hash": native_replay_hash(raw),
                       "replay_ignored_fields": ["top_level_sha256"],
                       "replay_uuid_normalization": ["init_id", "event_id", "position_id"]})
    raw = folded[-1]["raw"]
    # NAV observations from separately reset sleeves must NOT be concatenated
    # as if they were one continuous, compounding live account.
    metric_values = {name: value["value"] for name, value in raw["result"]["metrics"]["metrics"].items()}
    evidence = {
        "causality_verified": all(item["prefix_invariant"] and item["future_mutation_invariant"] for item in folded),
        "accounting_verified": all(item["raw"]["result"]["account_invariants"]["status"] == "verified"
                                   for item in folded),
        "dataset_verified": all(not service.datasets.load(dataset_id).gaps for dataset_id in spec.dataset_ids),
        "fee_coverage": 1,
        "reproducible": all(item["reproducible"] for item in folded),
        "oos_days": len(raw["result"]["metrics"]["daily_returns"]),
        **{key: value for key, value in metric_values.items() if value is not None},
    }
    result = {"schema_version": "2.0", "experiment_hash": identity,
              "strategy_hash": content_hash(strategy), "folds": folded,
              "gate_evidence": evidence, "engine_attestation": raw["image_attestation"],
              "limitations": ["fixed_candidate_fold_evaluation_not_parameter_optimization",
                              "fold_NAV_is_not_a_single_continuous_portfolio",
                              "forward_observation_and_full_statistical_acceptance_required"],
              "live_ready": False}
    with repo.transaction() as connection:
        repo.put(tenant, "experiment_result", result, identity,
                 fence_job=(job["job_id"], job["lease_token"]), connection=connection)
        service.experiments.finish_trial(tenant, identity, status="completed", result={
            "result_hash": content_hash(result), "gate_evidence": evidence,
        }, fence_job=(job["job_id"], job["lease_token"]), connection=connection)
    return {"experiment_hash": identity, "result_hash": content_hash(result),
            "paper_eligible": service.validation(identity, "paper")["eligible"]}


def tool_broker(service: ControlService, *, asof_ns: int | None = None,
                fence_job: tuple[str, str] | None = None):
    from qt.research.evidence import EvidenceRepository
    from qt.research.graph import ToolBroker

    evidence = EvidenceRepository(service.repository)
    observable_at = asof_ns if asof_ns is not None else time.time_ns()

    def lookup(tenant, *, kind: str, identity: str, offset: int = 0, length: int = 2000):
        """Read a known artifact as of run creation; large payloads use bounded JSON excerpts."""
        if kind not in {"strategy", "dataset", "instrument", "fees", "experiment_result"}:
            raise ValueError("artifact_kind_not_authorized")
        row = service.repository.get(tenant, kind, identity)
        if row["created_ns"] > observable_at:
            raise KeyError("artifact_not_yet_observable")
        serialized = json.dumps(row["payload"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        if (type(offset) is not int or not 0 <= offset <= len(serialized)
                or type(length) is not int or not 1 <= length <= 3000):
            raise ValueError("bounded_artifact_excerpt_required")
        metadata = {"id": identity, "kind": kind, "sha256": row["sha256"],
                    "recorded_at_ns": row["created_ns"], "knowledge_time_ns": observable_at}
        # Dataset manifest only; no raw file access or holdout data in model tools.
        if offset == 0 and len(serialized.encode()) <= 20_000:
            return {**metadata, "content": row["payload"], "is_excerpt": False}
        excerpt = serialized[offset:offset + length]
        end = offset + len(excerpt)
        return {**metadata, "content": None, "content_excerpt": excerpt,
                "content_format": "canonical_json_excerpt", "is_excerpt": True,
                "offset": offset, "next_offset": end if end < len(serialized) else None,
                "total_characters": len(serialized),
                "excerpt_sha256": content_hash({"artifact_sha256": row["sha256"], "offset": offset, "text": excerpt})}

    def catalog(tenant, *, kind: str, limit: int = 20):
        if kind not in {"strategy", "dataset", "instrument", "fees", "experiment_result"}:
            raise ValueError("artifact_kind_not_authorized")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("bounded_artifact_catalog_required")
        with service.repository.transaction() as connection:
            rows = connection.execute(select(artifacts).where(
                artifacts.c.tenant_id == tenant, artifacts.c.kind == kind,
                artifacts.c.created_ns <= observable_at,
            ).order_by(artifacts.c.created_ns.desc(), artifacts.c.entity_id).limit(limit)).mappings().all()
        if any(content_hash(row["payload"]) != row["sha256"] for row in rows):
            raise Conflict("artifact_integrity_failure")
        return {"knowledge_time_ns": observable_at,
                "items": [{"id": row["entity_id"], "sha256": row["sha256"], "recorded_at_ns": row["created_ns"]}
                          for row in rows]}

    def propose(tenant, *, proposal: dict):
        # Proposals are untrusted inert JSON. Executable versions require the
        # separate strict strategy registration and native-code allowlist.
        if len(str(proposal)) > 16_000:
            raise ValueError("proposal_too_large")
        row = service.repository.put(tenant, "strategy_proposal", proposal, fence_job=fence_job)
        return {"proposal_id": row["entity_id"], "execution_authorized": False}

    def request_experiment(tenant, *, spec: dict):
        if tenant != service.tenant:
            raise ValueError("tool_tenant_mismatch")
        job = service.create_experiment(spec, "research:" + content_hash(spec), fence_job=fence_job)
        return {"job_id": job["job_id"], "status": job["status"]}

    tools = {
        "artifacts.read": lookup, "artifacts.list": catalog,
        "evidence.search": lambda tenant, query, limit=5: evidence.search(
            tenant, query, asof_ns=observable_at, limit=limit),
        "evidence.read": lambda tenant, identity, offset=0, length=2000: evidence.read(
            tenant, identity, asof_ns=observable_at, offset=offset, length=length),
        "policy.read": lambda tenant: service.policy.definition(),
    }
    if fence_job is not None:
        tools.update({"strategies.propose": propose, "experiments.request": request_experiment})
    return ToolBroker(tools)


def paper_reconciliation_deployment(service: ControlService, account_id: str):
    """Only a signed active paper deployment may select this internal adapter."""
    if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 100:
        raise ValueError("account_id_required")
    selected = [row for row in service.active_deployments() if row["account_id"] == account_id]
    if len(selected) != 1:
        raise KeyError("active_account_deployment_not_found")
    manifest = service.verify_deployment(selected[0]["deployment_id"])
    if manifest.mode != "paper":
        raise PermissionError("paper_reconciliation_requires_paper_deployment")
    with service.repository.transaction() as connection:
        account = connection.execute(select(accounts).where(
            accounts.c.tenant_id == service.tenant, accounts.c.account_id == account_id,
        )).mappings().first()
    if account is None:
        raise KeyError("paper_account_not_initialized")
    if (account["capital"] != manifest.risk_budget.capital
            or account["quote_asset"] != manifest.risk_budget.currency):
        raise Conflict("paper_reconciliation_account_binding_changed")
    return manifest


async def reconcile_paper_once(service: ControlService) -> dict | None:
    """Consume one read-only paper reconciliation job; never submit/retry orders.

    Source results come exclusively from the server-owned durable PaperAdapter.
    Funded and shadow accounts cannot select a different adapter via job data.
    Pagination is explicit, with job cancellation/lease checks between pages.
    """
    from qt.execution.oms import OMS
    from qt.execution.simulated import PaperAdapter

    repo = service.repository
    job = repo.claim("reconciliation", lease_seconds=60, tenant=service.tenant)
    if job is None:
        return None
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(10):
            try:
                if not repo.heartbeat(job):
                    stopped.set()
            except Exception:
                stopped.set()

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()

    def active():
        current = repo.job(job["tenant_id"], job["job_id"])
        if current["cancel_requested"]:
            raise InterruptedError("reconciliation_cancelled")
        if (stopped.is_set() or current["status"] != "RUNNING"
                or current["lease_token"] != job["lease_token"]
                or current["lease_until_ns"] <= time.time_ns()
                or current["deadline_ns"] <= time.time_ns()):
            raise Conflict("reconciliation_lease_or_deadline_lost")

    try:
        payload = job["payload"]
        if (content_hash(payload) != job["sha256"] or set(payload) - {"account_id", "deployment_id"}
                or "account_id" not in payload):
            raise ValueError("reconciliation_job_payload_invalid")
        active()
        manifest = paper_reconciliation_deployment(service, payload["account_id"])
        if payload.get("deployment_id", manifest.deployment_id) != manifest.deployment_id:
            raise Conflict("reconciliation_deployment_superseded")
        adapter = PaperAdapter(repo, tenant_id=service.tenant)
        oms = OMS(repo)
        cursor = None
        pages = 0
        while True:
            active()
            current = paper_reconciliation_deployment(service, manifest.account_id)
            if current.deployment_id != manifest.deployment_id:
                raise Conflict("reconciliation_deployment_superseded")
            page = await oms.reconcile(service.tenant, manifest.account_id, adapter,
                                       venue=manifest.venue, cursor=cursor)
            pages += 1
            active()
            if page["complete"]:
                break
            next_cursor = page["next_cursor"]["cursor"]
            if next_cursor == cursor or pages >= 10_000:
                raise ValueError("reconciliation_pagination_did_not_finish")
            cursor = next_cursor
        return repo.finish(job, {
            "status": "reconciled", "account_id": manifest.account_id,
            "deployment_id": manifest.deployment_id, "pages": pages,
            "adapter": "durable_paper", "resubmitted_orders": 0,
            "funded_execution_authorized": False,
        })
    except Exception as error:
        result = {"status": "blocked" if isinstance(error, (KeyError, ValueError, PermissionError)) else "failed",
                  "error_type": type(error).__name__, "resubmitted_orders": 0,
                  "funded_execution_authorized": False}
        try:
            return repo.finish(job, result, failed=True)
        except Conflict:
            return {"status": "lease_lost", "job_id": job["job_id"], "resubmitted_orders": 0}
    finally:
        stopped.set()
        thread.join(timeout=2)


class _JobLease:
    """Bounded cancellation polling for CPU replays; all writes remain DB-fenced.

    Querying PostgreSQL for every market event makes large replays I/O-bound.
    Cache only this read-only cancellation hint for at most 100 ms. Deadlines,
    cached lease expiry and local shutdown are still checked on every call.
    A failed heartbeat invalidates the hint immediately instead of silently
    leaving a dead renewal thread behind.
    """

    def __init__(self, repository, job, *, shutdown=None, poll_seconds=.1):
        self.repository, self.job, self.shutdown = repository, job, shutdown
        self.stopped = threading.Event()
        self.current = job
        self.next_poll = 0.0
        self.poll_seconds = poll_seconds

    def active(self):
        if self.stopped.is_set() or (self.shutdown is not None and self.shutdown.is_set()):
            return False
        if time.monotonic() >= self.next_poll:
            self.current = self.repository.job(self.job["tenant_id"], self.job["job_id"])
            self.next_poll = time.monotonic() + self.poll_seconds
        current, now = self.current, time.time_ns()
        return (current["status"] == "RUNNING" and not current["cancel_requested"]
                and current["lease_token"] == self.job["lease_token"]
                and current["lease_until_ns"] > now and current["deadline_ns"] > now)

    def renew(self):
        try:
            if not self.repository.heartbeat(self.job):
                self.stopped.set()
        except Exception:
            # The worker must not keep consuming resources after losing its
            # ability to establish ownership. No exception/DSN is logged here.
            self.stopped.set()

    def heartbeat(self):
        while not self.stopped.wait(10):
            if self.shutdown is not None and self.shutdown.is_set():
                self.stopped.set()
                break
            self.renew()


async def _while_owned(awaitable, lease):
    """Cancel in-flight model I/O promptly on cancellation, shutdown or fencing."""
    task = asyncio.create_task(awaitable)
    try:
        while not task.done():
            if not await asyncio.to_thread(lease.active):
                raise InterruptedError("worker_cancelled_or_lease_lost")
            await asyncio.wait({task}, timeout=.1)
        return await task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


def run_once(service: ControlService, kind: str, *, provider=None, checkpointer=None,
             shutdown: threading.Event | None = None) -> dict | None:
    if kind not in {"experiment", "research"}:
        raise ValueError("unsupported_worker_job_kind")
    if shutdown is not None and shutdown.is_set():
        return None
    job = service.repository.claim(kind, lease_seconds=60, tenant=service.tenant)
    if kind == "experiment":
        service.experiments.reconcile_terminal_jobs(service.tenant)
    if job is None:
        return None
    lease = _JobLease(service.repository, job, shutdown=shutdown)
    thread = threading.Thread(target=lease.heartbeat, daemon=True)
    thread.start()
    try:
        if content_hash(job["payload"]) != job["sha256"]:
            raise ValueError("worker_job_payload_integrity_failure")
        if kind == "experiment":
            result = native_experiment(service, job, lambda: not lease.active())
        elif kind == "research":
            if provider is None:
                raise ValueError("research_requires_explicit_bounded_provider")
            from qt.research.graph import ResearchGraph

            async def research():
                if checkpointer is not None:
                    graph = ResearchGraph(service.repository, provider,
                                          tool_broker(service, asof_ns=job["created_ns"],
                                                      fence_job=(job["job_id"], job["lease_token"])), checkpointer)
                    return await graph.run(job, model_id=job["payload"]["model_id"])
                if service.repository.engine.dialect.name != "postgresql":
                    raise ValueError("production_research_requires_postgres_checkpointer")
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                url = service.repository.engine.url.set(drivername="postgresql")
                async with AsyncPostgresSaver.from_conn_string(url.render_as_string(hide_password=False)) as saver:
                    graph = ResearchGraph(service.repository, provider,
                                          tool_broker(service, asof_ns=job["created_ns"],
                                                      fence_job=(job["job_id"], job["lease_token"])), saver)
                    return await graph.run(job, model_id=job["payload"]["model_id"])
            result = asyncio.run(_while_owned(research(), lease))
        return service.repository.finish(job, result)
    except Exception as error:
        if shutdown is not None and shutdown.is_set():
            # Keep the attempt resumable after its lease expires. Already
            # persisted artifacts/call reservations remain immutable.
            return {"status": "interrupted", "job_id": job["job_id"]}
        if lease.stopped.is_set():
            return {"status": "lease_lost", "job_id": job["job_id"]}
        if isinstance(error, (SQLAlchemyError, OSError)) and not isinstance(error, InterruptedError):
            return {"status": "retry_pending", "job_id": job["job_id"], "error_type": type(error).__name__}
        if kind == "experiment":
            with suppress(KeyError, ValueError):
                service.experiments.finish_trial(job["tenant_id"], job["payload"]["experiment_hash"],
                                                status="failed", result={"error_type": type(error).__name__},
                                                fence_job=(job["job_id"], job["lease_token"]))
        try:
            finished = service.repository.finish(job, {"status": "failed", "error_type": type(error).__name__},
                                                 failed=True)
            if kind == "experiment":
                service.experiments.reconcile_terminal_jobs(service.tenant)
            return finished
        except Conflict:
            return {"status": "lease_lost", "job_id": job["job_id"]}
    finally:
        lease.stopped.set()
        thread.join(timeout=2)


def codex_provider_from_environment():
    from qt.agent_hub.codex import CodexProvider
    from qt.research.providers import CodexModelProvider

    model = os.environ.get("QT_V4_MODEL_ID")
    if not model:
        raise ValueError("QT_V4_MODEL_ID_required")
    return CodexModelProvider(CodexProvider(model=model),
                               input_usd_per_million=Decimal(os.environ["QT_V4_INPUT_USD_PER_MILLION"]),
                               output_usd_per_million=Decimal(os.environ["QT_V4_OUTPUT_USD_PER_MILLION"]))


def provider_from_environment():
    from qt.research.providers import LocalModelProvider

    provider = os.environ.get("QT_V4_MODEL_PROVIDER", "llama_cpp_local")
    if provider != "llama_cpp_local":
        raise ValueError("bounded_research_currently_requires_llama_cpp_local")
    return LocalModelProvider(os.environ["QT_V4_MODEL_ORIGIN"], model_id=os.environ["QT_V4_MODEL_ID"])
