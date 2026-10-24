"""Real PostgreSQL concurrency and durable graph tests, on the disposable DB only."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy.engine import make_url

from qt.control.service import ControlService
from qt.persistence import Conflict, Repository, create_engine, migrate
from tests.test_execution_oms import state as state

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres(tmp_path):
    address = os.environ.get("QT_V4_TEST_DATABASE_URL")
    if not address:
        pytest.skip("requires explicit isolated QT_V4_TEST_DATABASE_URL")
    url = make_url(address)
    if url.database != "qt_v4_test" or url.host not in {"127.0.0.1", "localhost", "postgres"}:
        pytest.fail("refusing a database that is not the dedicated qt_v4_test fixture")
    engine = create_engine(address)
    migrate(engine)
    tenant = "test-" + uuid.uuid4().hex
    service = ControlService(Repository(engine), tmp_path / "datasets", tenant=tenant,
                             signing_key=b"test-database-signing-key-32-characters")
    yield service
    engine.dispose()


def test_pg_idempotency_and_skip_locked_claims_across_independent_connections(postgres):
    service = postgres
    def enqueue(_):
        return Repository(service.repository.engine).enqueue(service.tenant, "concurrency", {"value": "fixed"}, "same")
    with ThreadPoolExecutor(max_workers=12) as executor:
        identifiers = {row["job_id"] for row in executor.map(enqueue, range(60))}
    assert len(identifiers) == 1
    for number in range(40):
        service.repository.enqueue(service.tenant, "concurrency", {"value": number}, str(number))
    def claim(_):
        return Repository(service.repository.engine).claim("concurrency")
    with ThreadPoolExecutor(max_workers=12) as executor:
        jobs = [job for job in executor.map(claim, range(60)) if job is not None]
    assert len(jobs) == 41 and len({job["job_id"] for job in jobs}) == 41
    for job in jobs:
        service.repository.finish(job, {"verified": True})


def test_pg_budget_serializes_concurrent_spend_and_unknown_calls_stay_reserved(postgres):
    from qt.operations.budgets import UsageLedger

    service = postgres
    def spend(number):
        usage = UsageLedger(Repository(service.repository.engine))
        try:
            usage.reserve(service.tenant, "run", str(number), "a" * 64, Decimal("0.20"), run_maximum=Decimal("1"))
            return "reserved"
        except (ValueError, Conflict):
            return "blocked"
    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(spend, range(30)))
    assert outcomes.count("reserved") == 5
    assert outcomes.count("blocked") == 25


def test_pg_async_graph_checkpoint_replays_without_a_second_model_call(postgres):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from qt.persistence.database import migrate_checkpoints
    from qt.research.graph import ResearchGraph, ToolBroker
    from tests.test_production_control import BoundedFixture

    migrate_checkpoints(postgres.repository.engine)
    repo, tenant = postgres.repository, postgres.tenant
    repo.enqueue(tenant, "pg-graph", {"question": "freeze"}, "checkpoint")
    job = repo.claim("pg-graph")
    provider = BoundedFixture()
    address = repo.engine.url.set(drivername="postgresql").render_as_string(hide_password=False)

    async def execute():
        async with AsyncPostgresSaver.from_conn_string(address) as saver:
            broker = ToolBroker({"evidence.read": lambda tenant, identity: {"id": identity, "source": "frozen"}})
            graph = ResearchGraph(repo, provider, broker, saver)
            first = await graph.run(job, model_id="frozen")
            calls = len(provider.calls)
        async with AsyncPostgresSaver.from_conn_string(address) as saver:
            restarted = ResearchGraph(repo, provider, broker, saver)
            second = await restarted.run(job, model_id="frozen")
            assert second == first and len(provider.calls) == calls
        return first
    assert asyncio.run(execute())["evidence_membership_verified"] is True


def test_pg_exact_money_and_single_account_writer(postgres):
    from qt.execution.oms import OMS

    service = postgres
    account_id = "pg-" + uuid.uuid4().hex
    OMS(service.repository).create_account(service.tenant, account_id, capital=Decimal("10000.000000000000000001"))
    def compete(number):
        try:
            return OMS(Repository(service.repository.engine)).claim_account(service.tenant, account_id, owner=str(number))
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=10) as executor:
        tokens = [value for value in executor.map(compete, range(30)) if value is not None]
    assert len(tokens) == 1
    assert OMS(service.repository).balances(service.tenant, account_id)["wallet"]["USDT"] == Decimal("10000.000000000000000001")


def test_pg_independent_instances_cannot_renew_by_reusing_the_same_owner_name(postgres):
    from qt.execution.oms import OMS

    service = postgres
    identity = "same-owner-" + uuid.uuid4().hex
    OMS(service.repository).create_account(service.tenant, identity, capital=Decimal("10000"))

    def compete(_):
        try:
            return OMS(Repository(service.repository.engine)).claim_account(
                service.tenant, identity, owner="identical-configured-owner",
            )
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        accepted = [token for token in executor.map(compete, range(40)) if token is not None]
    assert len(accepted) == 1
    replacement = OMS(Repository(service.repository.engine))
    with pytest.raises(Conflict, match="lease_held"):
        replacement.claim_account(service.tenant, identity, owner="identical-configured-owner")
    assert replacement.claim_account(service.tenant, identity, owner="identical-configured-owner",
                                     lease_token=accepted[0]) == accepted[0]


@pytest.mark.asyncio
async def test_pg_paper_depth_consumption_and_bounded_reconciliation_use_exact_persistent_history(postgres, state):
    from qt.execution.oms import OMS
    from qt.execution.simulated import PaperAdapter
    from tests.test_execution_oms import intent

    repo, tenant = postgres.repository, postgres.tenant
    identity = "pg-paper-" + uuid.uuid4().hex
    oms = OMS(repo)
    oms.create_account(tenant, identity, capital=Decimal("10000"))
    deployment = state.deployment.model_copy(update={"account_id": identity})
    token = oms.claim_account(tenant, identity, owner="paper-fixture")
    adapter = PaperAdapter(repo, tenant_id=tenant)
    adapter.set_book(state.market, state.instrument, state.fees, {
        "bids": [("9999", "1")], "asks": [("10001", "0.0015")],
    })
    for number in range(2):
        order = intent(state, identity + str(number), account_id=identity, quantity="0.001",
                       limit_price="10001", time_in_force="IOC")
        oms.reserve(tenant, order, instrument=state.instrument, fees=state.fees,
                    deployment=deployment, market=state.market)
        await oms.submit(tenant, order.intent_id, adapter, lease_token=token)
        assert (await oms.reconcile(tenant, identity, adapter, venue="fixture"))["complete"]
    assert oms.get_order(tenant, identity + "0")["status"] == "FILLED"
    assert oms.get_order(tenant, identity + "1")["status"] == "CANCELED"
    balances = oms.balances(tenant, identity)
    assert balances["wallet"]["BTC"] == Decimal("0.0015") and balances["reserved"]["USDT"] == 0


def test_pg_parallel_risk_reservations_cannot_overspend_or_duplicate_fills(postgres):
    from qt.contracts import (
        DeploymentManifest,
        ExecutableMarketSnapshot,
        FeeSchedule,
        InstrumentSpec,
        OrderIntent,
        RiskBudget,
    )
    from qt.execution.oms import OMS

    service, now = postgres, time.time_ns()
    account_id = "pg-risk-" + uuid.uuid4().hex
    OMS(service.repository).create_account(service.tenant, account_id, capital=Decimal("10000"))
    instrument = InstrumentSpec(venue="fixture", instrument="BTCUSDT", base_asset="BTC", quote_asset="USDT",
                                settlement_asset="USDT", price_increment="0.01", quantity_increment="0.0001",
                                minimum_quantity="0.0001", minimum_notional="1", effective_from_ns=0, source="fixture")
    fees = FeeSchedule(venue="fixture", account_tier="fixture", maker_rate="0.001", taker_rate="0.002",
                       fee_basis="notional", fee_asset="USDT", effective_from_ns=0, source="fixture", source_hash="a" * 64)
    manifest = DeploymentManifest(deployment_id="parallel", strategy_hash="a" * 64, provider="fixture", model_id="frozen",
                                   prompt_hash="a" * 64, tools_hash="a" * 64, risk_policy_hash="a" * 64,
                                   fee_schedule_hashes=(fees.content_hash(),), validation_report_hash="a" * 64,
                                   venue="fixture", account_id=account_id, mode="paper",
                                   risk_budget=RiskBudget(capital="10000", currency="USDT"), valid_from_ns=0,
                                   valid_until_ns=now + 60_000_000_000, signature="fixture-not-live")
    market = ExecutableMarketSnapshot(venue="fixture", instrument="BTCUSDT", bid="99.99", ask="100.01", asof_ns=now,
                                      book_valid=True, instrument_spec_hash=instrument.content_hash(), fee_schedule_hash=fees.content_hash())
    def reserve(number):
        identity = account_id + str(number)
        intent = OrderIntent(intent_id=identity, account_id=account_id, deployment_id="parallel", signal_id=str(number),
                              venue="fixture", instrument="BTCUSDT", side="buy", quantity="1", limit_price="100",
                              time_in_force="GTC", strategy_hash="a" * 64, policy_hash="a" * 64,
                              fee_schedule_hash=fees.content_hash(), instrument_spec_hash=instrument.content_hash(),
                              risk_reservation_id=identity, created_at_ns=now, valid_until_ns=now + 60_000_000_000)
        try:
            OMS(Repository(service.repository.engine)).reserve(service.tenant, intent, instrument=instrument, fees=fees,
                                                               deployment=manifest, market=market, now_ns=now)
            return identity
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=10) as executor:
        accepted = [identity for identity in executor.map(reserve, range(100)) if identity is not None]
    assert len(accepted) == 30
    assert OMS(service.repository).balances(service.tenant, account_id)["reserved"]["USDT"] == Decimal("3006")
    from qt.contracts import OrderIdentity, SubmitResult

    class PaperAck:
        execution_mode = "paper"

        async def submit(self, intent):
            return SubmitResult(status="accepted", identity=OrderIdentity(
                venue="fixture", account_id=account_id, intent_id=intent.intent_id,
                venue_order_id="pg-fake-order"), received_at_ns=time.time_ns())

    oms = OMS(service.repository)
    token = oms.claim_account(service.tenant, account_id, owner="pg-fill-test")
    asyncio.run(oms.submit(service.tenant, accepted[0], PaperAck(), lease_token=token, now_ns=now))
    def fill(_):
        return OMS(Repository(service.repository.engine)).apply_fill(
            service.tenant, accepted[0], venue_trade_id="same-fill", quantity=Decimal("1"), price=Decimal("100"),
            fee=Decimal("0.2"), fee_asset="USDT", base_asset="BTC", quote_asset="USDT")
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(fill, range(30)))
    balances = OMS(service.repository).balances(service.tenant, account_id)
    assert balances["wallet"] == {"USDT": Decimal("9899.8"), "BTC": Decimal("1")}
    assert balances["reserved"]["USDT"] == Decimal("2905.8")
