"""Fault regressions for queue atomicity, worker ownership and process readiness."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from qt.contracts import content_hash
from qt.control import worker
from qt.control.__main__ import trading_feed_configuration
from qt.control.api import create_app
from qt.control.service import ControlService
from qt.persistence import Conflict, Repository, create_engine, migrate
from qt.persistence.schema import artifacts, jobs, model_calls
from tests.test_production_review import _case


@pytest.fixture
def service(tmp_path):
    engine = create_engine("sqlite:///:memory:", allow_sqlite=True)
    migrate(engine)
    control = ControlService(Repository(engine), tmp_path / "data", tenant="stability",
                             signing_key=b"stability-test-only-signing-key-32-bytes")
    yield control
    engine.dispose()


def test_conflicting_experiment_request_does_not_consume_candidate_or_leave_spec(service):
    spec, _, _, _ = _case(service)
    original = service.create_experiment(spec, "same-key")
    assert 7199 < (original["deadline_ns"] - original["created_ns"]) / 1e9 <= 7200
    changed = {**spec, "seed": 19}
    with pytest.raises(Conflict):
        service.create_experiment(changed, "same-key")
    population = service.experiments.trial_population(service.tenant, spec["campaign_id"])
    assert len(population) == 1
    assert population[0]["trial_id"] == original["payload"]["experiment_hash"]
    assert len(service.repository.list(service.tenant, "experiment_spec")) == 1
    assert service.create_experiment(spec, "same-key") == original


def test_queue_failure_rolls_back_trial_spec_and_audit_together(service, monkeypatch):
    spec, _, _, _ = _case(service)

    def fail(*args, **kwargs):
        raise OperationalError("injected queue failure", {}, Exception("offline"))

    monkeypatch.setattr(service.repository, "enqueue", fail)
    with pytest.raises(OperationalError):
        service.create_experiment(spec, "failed-enqueue")
    assert service.experiments.trial_population(service.tenant, spec["campaign_id"]) == []
    assert service.repository.list(service.tenant, "experiment_spec") == []
    assert service.repository.event_page(service.tenant, content_hash(spec)) == []


def test_same_experiment_new_request_key_reuses_job_and_binds_every_key(service):
    spec, _, _, _ = _case(service)
    original = service.create_experiment(spec, "first")
    for key in ("second", "third", "first"):
        assert service.create_experiment(spec, key) == original
    for key in ("first", "second", "third"):
        with pytest.raises(Conflict):
            service.create_experiment({**spec, "seed": 91}, key)
    with service.repository.transaction() as connection:
        assert len(connection.execute(select(jobs)).all()) == 1
    assert len(service.experiments.trial_population(service.tenant, spec["campaign_id"])) == 1


def test_database_disconnection_leaves_experiment_recoverable(service, monkeypatch):
    spec, _, _, _ = _case(service)
    service.create_experiment(spec, "transient-error")

    def disconnected(*args):
        raise OperationalError("temporarily unavailable", {}, Exception("offline"))

    monkeypatch.setattr(worker, "native_experiment", disconnected)
    result = worker.run_once(service, "experiment")
    assert result["status"] == "retry_pending"
    assert service.repository.job(service.tenant, result["job_id"])["status"] == "RUNNING"
    assert service.experiments.trial_population(service.tenant, spec["campaign_id"])[0]["status"] == "submitted"


def test_cancelled_research_cannot_consume_trial_slot(service):
    spec, _, _, _ = _case(service)
    repo = service.repository
    repo.enqueue(service.tenant, "research", {"question": "fixture"}, "cancelled")
    job = repo.claim("research", tenant=service.tenant)
    repo.cancel(service.tenant, job["job_id"])
    with pytest.raises(Conflict):
        service.create_experiment(spec, "cancelled-tool", fence_job=(job["job_id"], job["lease_token"]))
    assert service.experiments.trial_population(service.tenant, spec["campaign_id"]) == []
    assert repo.list(service.tenant, "experiment_spec") == []


@pytest.mark.parametrize("terminal_reason", ["cancel", "exhausted"])
def test_abandoned_experiment_stays_counted_with_audited_terminal_state(service, terminal_reason):
    spec, _, _, _ = _case(service)
    job = service.create_experiment(spec, "abandoned")
    if terminal_reason == "cancel":
        service.repository.cancel(service.tenant, job["job_id"])
    else:
        with service.repository.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.job_id == job["job_id"]).values(deadline_ns=1))
    assert worker.run_once(service, "experiment") is None
    population = service.experiments.trial_population(service.tenant, spec["campaign_id"])
    assert len(population) == 1 and population[0]["status"] == "aborted"
    assert population[0]["result"]["queue_status"] == ("CANCELLED" if terminal_reason == "cancel" else "EXHAUSTED")
    assert service.experiments.reconcile_terminal_jobs(service.tenant) == 0


def test_stale_worker_cannot_poison_successor_trial(service, monkeypatch):
    spec, _, _, _ = _case(service)
    requested = service.create_experiment(spec, "steal-lease")
    successor = []

    def interrupted(control, job, cancelled):
        with control.repository.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.job_id == job["job_id"]).values(lease_until_ns=0))
        successor.append(control.repository.claim("experiment", tenant=control.tenant))
        raise InterruptedError("old computation lost ownership")

    monkeypatch.setattr(worker, "native_experiment", interrupted)
    result = worker.run_once(service, "experiment")
    assert result["status"] == "lease_lost"
    population = service.experiments.trial_population(service.tenant, spec["campaign_id"])
    assert population[0]["status"] == "submitted"
    next_job = successor[0]
    service.experiments.finish_trial(service.tenant, requested["payload"]["experiment_hash"],
                                    status="completed", result={"fixture": "new-owner"},
                                    fence_job=(next_job["job_id"], next_job["lease_token"]))


def test_experiment_result_and_trial_finalization_are_atomic(service, monkeypatch):
    spec, _, _, _ = _case(service)
    requested = service.create_experiment(spec, "result-atomicity")
    job = service.repository.claim("experiment", tenant=service.tenant)
    identity = requested["payload"]["experiment_hash"]
    # The injected engine isolates a crash between durable result and terminal
    # trial publication; actual engine behavior has its own native replay tests.
    import qt.experiments.native as native

    monkeypatch.setattr(native, "run", lambda *a, **kw: {
        "actual_receipt_delay_ns": 0, "image_attestation": "fixture",
        "result": {"metrics": {"metrics": {}, "daily_returns": []},
                   "account_invariants": {"status": "verified"}},
    })

    def crash(*args, **kwargs):
        raise RuntimeError("crash before terminal trial commit")

    monkeypatch.setattr(service.experiments, "finish_trial", crash)
    with pytest.raises(RuntimeError):
        worker.native_experiment(service, job, lambda: False)
    with pytest.raises(KeyError):
        service.repository.get(service.tenant, "experiment_result", identity)
    assert service.experiments.trial_population(service.tenant, spec["campaign_id"])[0]["status"] == "submitted"


def test_cpu_event_cancellation_polling_is_bounded_but_deadlines_are_immediate(monkeypatch):
    clocks = {"steady": 0.0, "wall": 100}
    job = {"tenant_id": "t", "job_id": "j", "status": "RUNNING", "cancel_requested": 0,
           "lease_token": "owned", "lease_until_ns": 1000, "deadline_ns": 2000}
    reads = []

    def get(*args):
        reads.append(args)
        return dict(job)

    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clocks["steady"],
                                                       time_ns=lambda: clocks["wall"]))
    lease = worker._JobLease(SimpleNamespace(job=get), dict(job))
    assert all(lease.active() for _ in range(100_000))
    assert len(reads) == 1
    job["cancel_requested"] = 1
    clocks["steady"] = .101
    assert not lease.active() and len(reads) == 2
    job["cancel_requested"] = 0
    clocks["steady"] = .202
    assert lease.active()
    clocks["wall"] = 1001
    assert not lease.active()  # expiry is checked even inside cached probe window


def test_heartbeat_exception_immediately_invalidates_ownership(service, monkeypatch):
    service.repository.enqueue(service.tenant, "experiment", {"experiment_hash": "fixture"}, "heartbeat")
    lease = worker._JobLease(service.repository, service.repository.claim("experiment"))
    assert lease.active()

    def disconnected(*args):
        raise OSError("database disconnected")

    monkeypatch.setattr(service.repository, "heartbeat", disconnected)
    lease.renew()
    assert not lease.active()


@pytest.mark.parametrize("stop_kind", ["cancel", "shutdown"])
def test_inflight_research_stops_promptly_without_reissuing_or_refunding_unknown_call(service, stop_kind):
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import InMemorySaver

    started, stopped, shutdown = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class HangingProvider:
        enforces_token_limits = True

        async def invoke(self, request, budget):
            calls.append(request)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    repo = service.repository
    job = repo.enqueue(service.tenant, "research", {"question": "fixture", "model_id": "frozen"}, "inflight")
    result = []
    thread = threading.Thread(target=lambda: result.append(worker.run_once(
        service, "research", provider=HangingProvider(), checkpointer=InMemorySaver(), shutdown=shutdown,
    )), daemon=True)
    thread.start()
    try:
        assert started.wait(5)
        if stop_kind == "cancel":
            repo.cancel(service.tenant, job["job_id"])
        else:
            shutdown.set()
        thread.join(timeout=2)
        assert not thread.is_alive() and stopped.is_set() and len(calls) == 1
        assert result[0]["status"] == ("CANCELLED" if stop_kind == "cancel" else "interrupted")
        with repo.transaction() as connection:
            receipt = connection.execute(select(model_calls)).mappings().one()
            assert receipt["status"] == "RESERVED" and receipt["actual"] is None
            assert connection.execute(select(artifacts).where(artifacts.c.kind == "model_result")).first() is None
    finally:
        shutdown.set()
        thread.join(timeout=2)


def test_trading_feed_uses_validated_strategy_interval_and_instrument(monkeypatch):
    monkeypatch.setenv("QT_V4_SYMBOL", "BTCUSDT")
    monkeypatch.setenv("QT_V4_BAR_INTERVAL", "1m")
    runtime = SimpleNamespace(strategy=SimpleNamespace(artifact=SimpleNamespace(signal_interval_ns=3_600_000_000_000)))
    instrument = SimpleNamespace(venue="binance", instrument_type="spot", instrument="ETHUSDT")
    assert trading_feed_configuration(runtime, instrument) == {"symbol": "ETHUSDT", "bar_interval": "1h"}
    instrument.venue = "polymarket"
    with pytest.raises(ValueError, match="binance_spot"):
        trading_feed_configuration(runtime, instrument)


@pytest.mark.asyncio
async def test_data_worker_once_exits_after_durable_manifest_on_healthy_connection(service, monkeypatch):
    from qt.control.__main__ import collect
    from qt.marketdata import collector

    _, _, _, manifest = _case(service)
    captured = []

    class ConnectedCollector:
        def __init__(self, registry, **kwargs):
            self.callbacks = kwargs

        async def run(self, *, stop, maximum_sessions):
            assert maximum_sessions == 1
            await self.callbacks["on_event"](None, None)
            assert not stop.is_set()
            result = self.callbacks["on_manifest"](manifest.model_dump(mode="json"))
            captured.append(result)
            await asyncio.wait_for(stop.wait(), timeout=.25)

    monkeypatch.setattr(collector, "BinanceCollector", ConnectedCollector)
    await collect(service, once=True)
    assert captured[0]["entity_id"] == manifest.dataset_id


@pytest.mark.asyncio
async def test_reconciliation_background_survives_failed_health_reporting(service, monkeypatch):
    from qt.control.__main__ import reconcile_background

    stop, loop, calls = asyncio.Event(), asyncio.get_running_loop(), []

    async def attempt(control):
        calls.append(control.tenant)
        if len(calls) == 2:
            loop.call_soon_threadsafe(stop.set)
        raise OperationalError("database unavailable", {}, Exception("offline"))

    def fail_health(*args):
        raise OperationalError("health database also unavailable", {}, Exception("offline"))

    monkeypatch.setattr(worker, "reconcile_paper_once", attempt)
    monkeypatch.setattr(service.repository, "health", fail_health)
    await asyncio.wait_for(reconcile_background(service, stop), timeout=3)
    assert calls == [service.tenant, service.tenant]


def test_direct_api_factory_uses_same_tenant_and_budget_as_worker(service, monkeypatch):
    import qt.control.api as api

    monkeypatch.setenv("QT_V4_TENANT", "configured-tenant")
    monkeypatch.setenv("QT_V4_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("QT_V4_DATA_ROOT", str(service.datasets.root))
    monkeypatch.setenv("QT_V4_EXPERIMENT_TIMEOUT_SECONDS", "3600")
    monkeypatch.setattr(api, "create_engine", lambda url: service.repository.engine)
    app = api.create_app(token="test-only")
    assert app.state.service.tenant == "configured-tenant"
    assert app.state.service.experiment_timeout_seconds == 3600


def test_readiness_requires_current_database_migrations_and_recovers(service, monkeypatch):
    client = TestClient(create_app(service, token="test-only"))
    assert client.get("/healthz").status_code == client.get("/readyz").status_code == 200
    with service.repository.transaction() as connection:
        head = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        connection.execute(text("UPDATE alembic_version SET version_num='0001_v4'"))
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    with service.repository.transaction() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num=:head"), {"head": head})
    assert client.get("/readyz").status_code == 200

    @contextmanager
    def offline():
        raise OperationalError("password=never-print-this", {}, Exception("private-connection"))
        yield  # pragma: no cover

    with monkeypatch.context() as patch:
        patch.setattr(service.repository, "transaction", offline)
        unavailable = client.get("/readyz")
        assert unavailable.status_code == 503 and unavailable.headers["Retry-After"] == "5"
        assert "never-print" not in unavailable.text and "private-connection" not in unavailable.text
    assert client.get("/readyz").status_code == 200
