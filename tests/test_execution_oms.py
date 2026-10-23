"""Fault-oriented acceptance tests for the new durable OMS, without venue calls."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from qt.contracts import (
    CancelResult,
    DeploymentManifest,
    ExecutableMarketSnapshot,
    FeeSchedule,
    InstrumentSpec,
    OrderIdentity,
    OrderIntent,
    OrderSnapshot,
    RiskBudget,
    SubmitResult,
)
from qt.execution.oms import OMS
from qt.persistence import Conflict, Repository, create_engine, migrate
from qt.persistence.schema import accounts, ledger_entries, ledger_transactions, orders

_D = Decimal
_NOW = 1_700_000_000_000_000_000
_HASH = "a" * 64


class Adapter:
    execution_mode = "paper"

    def __init__(self, *, submit="accepted", cancel="confirmed"):
        self.submit_status = submit
        self.cancel_status = cancel
        self.submits = 0
        self.cancels = 0
        self.snapshot = None
        self.exception = None
        self.during_submit = None

    async def submit(self, intent):
        self.submits += 1
        if self.during_submit:
            self.during_submit(intent)
        if self.exception:
            raise self.exception
        return SubmitResult(
            status=self.submit_status,
            identity=OrderIdentity(venue=intent.venue, account_id=intent.account_id,
                                   intent_id=intent.intent_id, venue_order_id="venue-" + intent.intent_id),
            received_at_ns=_NOW,
        )

    async def cancel(self, identity):
        self.cancels += 1
        if self.exception:
            raise self.exception
        return CancelResult(status=self.cancel_status, identity=identity, received_at_ns=_NOW)

    async def query(self, identity):
        if self.exception:
            raise self.exception
        return self.snapshot


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr("qt.execution.oms.time.time_ns", lambda: _NOW)
    engine = create_engine("sqlite:///:memory:", allow_sqlite=True)
    migrate(engine)
    repo = Repository(engine)
    oms = OMS(repo)
    oms.create_account("owner", "paper-account", capital=_D("10000"))
    instrument = InstrumentSpec(
        venue="fixture", instrument="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        settlement_asset="USDT", price_increment="0.01", quantity_increment="0.0001",
        minimum_quantity="0.0001", minimum_notional="1", effective_from_ns=0,
        source="fixture-only",
    )
    fees = FeeSchedule(
        venue="fixture", account_tier="fixture", maker_rate="0.001", taker_rate="0.002",
        fee_basis="notional", fee_asset="USDT", effective_from_ns=0,
        source="fixture-only", source_hash=_HASH,
    )
    deployment = DeploymentManifest(
        deployment_id="test-deployment", strategy_hash=_HASH, provider="fixture", model_id="frozen",
        prompt_hash=_HASH, tools_hash=_HASH, risk_policy_hash=_HASH,
        fee_schedule_hashes=(fees.content_hash(),), validation_report_hash=_HASH,
        venue="fixture", account_id="paper-account", mode="paper",
        risk_budget=RiskBudget(capital="10000", currency="USDT"),
        valid_from_ns=0, valid_until_ns=_NOW + 1_000_000_000_000, signature="test-only",
    )
    market = ExecutableMarketSnapshot(
        venue="fixture", instrument="BTCUSDT", bid="9999", ask="10001", asof_ns=_NOW,
        book_valid=True, instrument_spec_hash=instrument.content_hash(), fee_schedule_hash=fees.content_hash(),
    )
    yield SimpleNamespace(oms=oms, repo=repo, instrument=instrument, fees=fees,
                          deployment=deployment, market=market)
    engine.dispose()


def intent(state, identity="one", **extra):
    return OrderIntent(
        **{"intent_id": identity, "account_id": "paper-account", "deployment_id": "test-deployment",
           "signal_id": "signal-" + identity, "venue": "fixture", "instrument": "BTCUSDT",
           "side": "buy", "quantity": "0.01", "limit_price": "10000", "time_in_force": "GTC",
           "strategy_hash": _HASH, "policy_hash": _HASH, "fee_schedule_hash": state.fees.content_hash(),
           "instrument_spec_hash": state.instrument.content_hash(), "risk_reservation_id": "reservation-" + identity,
           "created_at_ns": _NOW, "valid_until_ns": _NOW + 60_000_000_000, **extra}
    )


def reserve(state, identity="one", **extra):
    return state.oms.reserve("owner", intent(state, identity, **extra), instrument=state.instrument,
                             fees=state.fees, deployment=state.deployment, market=state.market)


def fill(state, identity="one", trade="trade-one", **extra):
    return state.oms.apply_fill(
        "owner", identity,
        **{"venue_trade_id": trade, "quantity": _D("0.01"), "price": _D("10000"),
           "fee": _D("0.2"), "fee_asset": "USDT", "base_asset": "BTC", "quote_asset": "USDT", **extra},
    )


def lease(state, owner="executor"):
    return state.oms.claim_account("owner", "paper-account", owner=owner)


@pytest.mark.asyncio
async def test_partial_fill_cancel_and_late_fill_keep_exact_balanced_wallet(state):
    reserved = reserve(state)
    assert reserved["reserved_quote"] == _D("100.2")
    adapter = Adapter()
    token = lease(state)
    sent = await state.oms.submit("owner", "one", adapter, lease_token=token)
    assert sent["status"] == "ACKNOWLEDGED"
    fill(state, quantity=_D("0.004"), fee=_D("0.08"))
    assert state.oms.get_order("owner", "one")["reserved_quote"] == _D("60.12")
    assert state.oms.balances("owner", "paper-account")["wallet"] == {"USDT": _D("9959.92"), "BTC": _D("0.004")}
    canceled = await state.oms.cancel("owner", "one", adapter, lease_token=token)
    assert canceled["status"] == "CANCELED"
    assert state.oms.balances("owner", "paper-account")["reserved"]["USDT"] == 0
    fill(state, trade="late", quantity=_D("0.002"), fee=_D("0.04"))
    order = state.oms.get_order("owner", "one")
    assert order["status"] == "CANCELED"
    assert order["filled_quantity"] == _D("0.006")
    balances = state.oms.balances("owner", "paper-account")
    assert balances["wallet"] == {"USDT": _D("9939.88"), "BTC": _D("0.006")}
    assert balances["mode"] == "ACTIVE"
    with state.repo.transaction() as connection:
        grouped = {}
        for row in connection.execute(select(ledger_entries)).mappings():
            key = row["transaction_id"], row["asset"]
            grouped[key] = grouped.get(key, _D(0)) + row["amount"]
        assert not any(grouped.values())


@pytest.mark.asyncio
async def test_timeout_unknown_restart_and_notfound_never_resend_or_release(state):
    reserve(state)
    adapter = Adapter()
    adapter.exception = TimeoutError("fixture")
    token = lease(state)
    uncertain = await state.oms.submit("owner", "one", adapter, lease_token=token)
    assert uncertain["status"] == "UNKNOWN"
    restarted = OMS(state.repo)
    replay = await restarted.submit("owner", "one", adapter, lease_token=token)
    assert replay["requires_reconciliation"] is True
    adapter.exception = KeyError("not_found")
    queried = await restarted.query("owner", "one", adapter)
    assert queried["status"] == "UNKNOWN"
    assert queried["reserved_quote"] == _D("100.2")
    assert adapter.submits == 1
    with pytest.raises(ValueError, match="uncertain_orders"):
        restarted.set_mode("owner", "paper-account", "ACTIVE", "cannot clear uncertainty")
    with pytest.raises(ValueError, match="unresolved_execution"):
        restarted.mark_reconciled("owner", "paper-account")


@pytest.mark.asyncio
async def test_crash_after_submitting_requires_query_before_retry(state):
    reserve(state)
    with state.repo.transaction() as connection:
        connection.execute(update(orders).where(orders.c.intent_id == "one").values(status="SUBMITTING"))
    adapter = Adapter()
    result = await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    assert result["requires_reconciliation"] is True
    assert adapter.submits == 0


@pytest.mark.asyncio
async def test_duplicate_fills_and_conflicting_payloads(state):
    reserve(state)
    await state.oms.submit("owner", "one", Adapter(), lease_token=lease(state))
    first = fill(state)
    assert fill(state) == first
    with pytest.raises(Conflict, match="venue_trade_identity_conflict"):
        fill(state, price=_D("10001"))
    assert state.oms.balances("owner", "paper-account")["wallet"]["USDT"] == _D("9899.8")
    assert len(state.oms.get_order("owner", "one")["fills"]) == 1


def test_reservation_identity_and_versions_are_immutable(state):
    first = reserve(state)
    assert reserve(state) == first
    with pytest.raises(Conflict, match="order_identity_conflict"):
        reserve(state, quantity="0.009")
    with pytest.raises(Conflict, match="signal_leg_already_reserved"):
        reserve(state, "two", signal_id="signal-one")
    with pytest.raises(ValueError, match="version_mismatch"):
        reserve(state, "wrong", instrument_spec_hash="b" * 64)


@pytest.mark.parametrize("values", [
    {"quantity": "0.0201"}, {"quantity": "0.01001"}, {"limit_price": "10000.001"},
    {"quantity": "0.00001"}, {"valid_until_ns": _NOW - 1},
])
def test_size_precision_and_expiry_fail_closed(state, values):
    if values.get("valid_until_ns", _NOW + 1) <= _NOW:
        values["created_at_ns"] = _NOW - 2
    with pytest.raises(ValueError):
        reserve(state, **values)
    assert state.oms.balances("owner", "paper-account")["reserved"]["USDT"] == 0


def test_market_health_and_unknown_fees_fail_closed(state):
    state.market = state.market.model_copy(update={"book_valid": False})
    with pytest.raises(ValueError, match="untrusted_or_stale_market"):
        reserve(state)
    state.market = state.market.model_copy(update={"book_valid": True, "asof_ns": _NOW - 2_000_000_000})
    with pytest.raises(ValueError, match="untrusted_or_stale_market"):
        reserve(state)
    assert state.oms.balances("owner", "paper-account")["mode"] == "HALTED"


@pytest.mark.asyncio
async def test_single_writer_lease_and_exactly_one_submit(state):
    reserve(state)
    token = lease(state)
    assert lease(state) == token
    with pytest.raises(Conflict, match="lease_held"):
        lease(state, owner="another-executor")
    adapter = Adapter()
    await state.oms.submit("owner", "one", adapter, lease_token=token)
    await state.oms.submit("owner", "one", adapter, lease_token=token)
    assert adapter.submits == 1
    with pytest.raises(Conflict, match="lease_lost"):
        await state.oms.submit("owner", "one", adapter, lease_token="wrong")


@pytest.mark.asyncio
async def test_rejection_releases_but_unknown_cancel_does_not(state):
    reserve(state)
    token = lease(state)
    rejected = await state.oms.submit("owner", "one", Adapter(submit="rejected"), lease_token=token)
    assert rejected["status"] == "REJECTED" and rejected["reserved_quote"] == 0
    reserve(state, "two")
    adapter = Adapter(cancel="unknown")
    await state.oms.submit("owner", "two", adapter, lease_token=token)
    canceled = await state.oms.cancel("owner", "two", adapter, lease_token=token)
    assert canceled["status"] == "CANCEL_PENDING"
    assert canceled["reserved_quote"] == _D("100.2")


@pytest.mark.asyncio
async def test_funded_path_denied_and_shadow_never_sends(state):
    state.deployment = state.deployment.model_copy(update={"mode": "live_canary"})
    with pytest.raises(ValueError, match="funded_execution_not_verified"):
        reserve(state)
    state.deployment = state.deployment.model_copy(update={"mode": "shadow"})
    reserve(state)
    adapter = Adapter()
    result = await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    assert result["shadow"] is True and result["sent"] is False
    assert adapter.submits == 0


@pytest.mark.asyncio
async def test_paper_never_uses_an_unlabelled_or_funded_adapter(state):
    reserve(state)
    adapter = Adapter()
    adapter.execution_mode = "live"
    with pytest.raises(ValueError, match="explicit_paper_adapter"):
        await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    assert adapter.submits == 0
    assert state.oms.get_order("owner", "one")["status"] == "RESERVED"


@pytest.mark.asyncio
async def test_owned_inventory_sell_reduces_risk_when_daily_entries_exhausted(state):
    reserve(state)
    token = lease(state)
    await state.oms.submit("owner", "one", Adapter(), lease_token=token)
    fill(state)
    with state.repo.transaction() as connection:
        connection.execute(update(accounts).where(accounts.c.account_id == "paper-account").values(daily_new_risk=_D("10000")))
    state.oms.set_mode("owner", "paper-account", "REDUCE_ONLY", "protective exit")
    reserve(state, "exit", side="sell", reduce_only=True)
    with pytest.raises(ValueError, match="owned_inventory"):
        reserve(state, "double-exit", side="sell", reduce_only=True)
    await state.oms.submit("owner", "exit", Adapter(), lease_token=token)
    fill(state, "exit", "sell-trade")
    assert state.oms.balances("owner", "paper-account")["wallet"]["BTC"] == 0


@pytest.mark.asyncio
async def test_balance_is_derived_from_fills_not_requested_quantity(state):
    reserve(state)
    assert state.oms.balances("owner", "paper-account")["wallet"].get("BTC", 0) == 0
    await state.oms.submit("owner", "one", Adapter(), lease_token=lease(state))
    fill(state, quantity=_D("0.001"), fee=_D("0.02"))
    assert state.oms.balances("owner", "paper-account")["wallet"]["BTC"] == _D("0.001")


@pytest.mark.asyncio
async def test_fill_arrives_before_submission_response_preserves_fill_state(state):
    reserve(state)
    adapter = Adapter()
    adapter.during_submit = lambda _: fill(state)
    result = await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    assert result["status"] == "FILLED"
    assert result["venue_order_id"] == "venue-one"
    assert result["reserved_quote"] == 0


@pytest.mark.asyncio
async def test_authoritative_query_resolves_unknown_and_keeps_budget_until_terminal(state):
    reserve(state)
    adapter = Adapter(submit="unknown")
    token = lease(state)
    await state.oms.submit("owner", "one", adapter, lease_token=token)
    adapter.snapshot = OrderSnapshot(
        identity=OrderIdentity(venue="fixture", account_id="paper-account", intent_id="one", venue_order_id="venue-one"),
        status="ACKNOWLEDGED", quantity="0.01", filled_quantity="0", asof_ns=_NOW,
    )
    result = await state.oms.query("owner", "one", adapter)
    assert result["status"] == "ACKNOWLEDGED"
    assert result["reserved_quote"] == _D("100.2")
    state.oms.mark_reconciled("owner", "paper-account")
    state.oms.set_mode("owner", "paper-account", "ACTIVE", "verified query and account reconciliation")
    adapter.snapshot = adapter.snapshot.model_copy(update={"status": "CANCELED"})
    result = await state.oms.query("owner", "one", adapter)
    assert result["reserved_quote"] == 0
    assert result["status"] == "CANCELED"


@pytest.mark.asyncio
async def test_unseen_fills_prevent_query_from_releasing_reserved_cash(state):
    reserve(state)
    adapter = Adapter()
    await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    adapter.snapshot = OrderSnapshot(
        identity=OrderIdentity(venue="fixture", account_id="paper-account", intent_id="one", venue_order_id="venue-one"),
        status="CANCELED", quantity="0.01", filled_quantity="0.001", asof_ns=_NOW,
    )
    result = await state.oms.query("owner", "one", adapter)
    assert result["status"] == "UNKNOWN"
    assert result["reserved_quote"] == _D("100.2")


@pytest.mark.asyncio
async def test_original_fee_currency_and_settlement_failures_are_preserved(state):
    reserve(state)
    await state.oms.submit("owner", "one", Adapter(), lease_token=lease(state))
    applied = fill(state, fee=_D("0.00002"), fee_asset="BTC", settlement_status="REORGED")
    assert applied["fee_asset"] == "BTC"
    assert state.oms.balances("owner", "paper-account")["wallet"]["BTC"] == _D("0.00998")
    assert state.oms.balances("owner", "paper-account")["mode"] == "HALTED"
    assert state.oms.get_order("owner", "one")["settlement_status"] == "REORGED"


def test_decimal_text_does_not_round_through_binary_float(state):
    exact = _D("0.100000000000000001")
    state.oms.create_account("owner", "precise", capital=exact)
    assert state.oms.balances("owner", "precise")["wallet"]["USDT"] == exact
    with state.repo.transaction() as connection:
        assert connection.execute(select(ledger_transactions).where(ledger_transactions.c.account_id == "precise")).first()


def test_tenant_isolation_and_float_inputs_are_rejected(state):
    reserve(state)
    with pytest.raises(KeyError, match="order_not_found"):
        state.oms.get_order("another", "one")
    with pytest.raises(KeyError, match="account_not_found"):
        state.oms.balances("another", "paper-account")
    with pytest.raises(ValueError, match="decimal"):
        state.oms.create_account("owner", "invalid", capital=0.1)


def book_adapter(state, *, persistent=True, asks=None):
    from qt.execution.simulated import PaperAdapter
    adapter = PaperAdapter(state.repo if persistent else None, tenant_id="owner")
    adapter.set_book(state.market, state.instrument, state.fees, {
        "bids": [("9999", "0.01")], "asks": asks or [("10001", "0.005"), ("10002", "0.004")],
    })
    return adapter


@pytest.mark.asyncio
async def test_paper_consumes_supplied_depth_then_reconciles_durable_fills(state):
    reserve(state, limit_price="10002", time_in_force="IOC")
    adapter = book_adapter(state)
    await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    result = await state.oms.reconcile("owner", "paper-account", adapter, venue="fixture")
    assert result["complete"] is True
    row = state.oms.get_order("owner", "one")
    assert row["status"] == "CANCELED"
    assert row["filled_quantity"] == _D("0.009")
    assert [part["price"] for part in row["fills"]] == [_D("10001"), _D("10002")]
    assert row["reserved_quote"] == 0
    # Duplicate replay is safe and the same visible depth cannot fill twice.
    await state.oms.reconcile("owner", "paper-account", adapter, venue="fixture")
    assert len(state.oms.get_order("owner", "one")["fills"]) == 2
    reserve(state, "two", limit_price="10002", time_in_force="IOC")
    await state.oms.submit("owner", "two", adapter, lease_token=lease(state))
    await state.oms.reconcile("owner", "paper-account", adapter, venue="fixture")
    assert state.oms.get_order("owner", "two")["filled_quantity"] == 0


@pytest.mark.asyncio
async def test_paper_fok_has_no_partial_side_effect_or_depth_consumption(state):
    adapter = book_adapter(state)
    rejected = await adapter.submit(intent(state, time_in_force="FOK", limit_price="10002"))
    assert rejected.status == "rejected"
    response = await adapter.submit(intent(state, "two", quantity="0.005", time_in_force="FOK", limit_price="10001"))
    assert response.status == "accepted"
    snapshot = await adapter.query(response.identity)
    assert snapshot.status == "FILLED" and snapshot.filled_quantity == _D("0.005")


@pytest.mark.asyncio
async def test_paper_respects_limit_price_and_does_not_invent_maker_fills(state):
    adapter = book_adapter(state)
    response = await adapter.submit(intent(state, limit_price="10000"))
    snapshot = await adapter.query(response.identity)
    assert snapshot.status == "ACKNOWLEDGED" and snapshot.filled_quantity == 0
    assert (await adapter.cancel(response.identity)).status == "confirmed"
    assert (await adapter.query(response.identity)).status == "CANCELED"


@pytest.mark.asyncio
async def test_paper_result_survives_crash_before_oms_acknowledgement(state):
    from qt.execution.simulated import PaperAdapter
    reserve(state, limit_price="10001", quantity="0.005", time_in_force="FOK")
    order = intent(state, limit_price="10001", quantity="0.005", time_in_force="FOK")
    with state.repo.transaction() as connection:
        connection.execute(update(orders).where(orders.c.intent_id == "one").values(status="SUBMITTING"))
    adapter = book_adapter(state)
    first_response = await adapter.submit(order)
    restarted = PaperAdapter(state.repo, tenant_id="owner")
    assert await restarted.submit(order) == first_response
    result = await state.oms.reconcile("owner", "paper-account", restarted, venue="fixture")
    assert result["complete"] is True
    assert state.oms.get_order("owner", "one")["status"] == "FILLED"
    assert state.oms.balances("owner", "paper-account")["wallet"]["BTC"] == _D("0.005")


@pytest.mark.asyncio
async def test_paper_restart_does_not_replenish_same_snapshot_depth(state):
    first_adapter = book_adapter(state)
    await first_adapter.submit(intent(state, quantity="0.005", limit_price="10001", time_in_force="FOK"))
    restarted = book_adapter(state)
    result = await restarted.submit(intent(state, "two", quantity="0.005", limit_price="10001", time_in_force="FOK"))
    assert result.status == "rejected"


@pytest.mark.asyncio
async def test_paper_reused_intent_identity_cannot_change_quantity(state):
    adapter = book_adapter(state)
    await adapter.submit(intent(state))
    with pytest.raises(Conflict, match="paper_intent_identity_conflict"):
        await adapter.submit(intent(state, quantity="0.009"))


@pytest.mark.asyncio
async def test_paper_requires_real_depth_not_unbounded_quote(state):
    from qt.execution.simulated import PaperAdapter
    with pytest.raises(ValueError, match="supplied_depth"):
        await PaperAdapter().submit(intent(state))
    with pytest.raises(ValueError, match="quote_or_version"):
        book_adapter(state, asks=[("10002", "999999")])


@pytest.mark.asyncio
async def test_shadow_adapter_never_creates_an_order(state):
    from qt.contracts import ReconcileCursor
    from qt.execution.simulated import ShadowAdapter
    adapter = ShadowAdapter()
    result = await adapter.submit(intent(state))
    assert result.status == "unknown" and result.reason == "shadow_unsent"
    with pytest.raises(KeyError, match="no_venue_order"):
        await adapter.query(result.identity)
    assert (await adapter.reconcile(ReconcileCursor(venue="fixture", account_id="paper-account"))).orders == ()


@pytest.mark.parametrize("value", ["0.0000000000000000001", "0.1000000000000000001", "100000000000000000000"])
def test_numeric_binding_rejects_implicit_rounding_on_both_backend_dialects(value):
    from sqlalchemy.dialects import postgresql, sqlite

    from qt.persistence.schema import ExactMoney
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        with pytest.raises(ValueError, match="fit_numeric"):
            ExactMoney().process_bind_param(Decimal(value), dialect)


def test_numeric_binding_preserves_the_same_exact_value_across_backend_dialects():
    from sqlalchemy.dialects import postgresql, sqlite

    from qt.persistence.schema import ExactMoney
    for source in ("0.100000000000000001", "99999999999999999999.999999999999999999"):
        for dialect in (postgresql.dialect(), sqlite.dialect()):
            binding = ExactMoney().process_bind_param(Decimal(source), dialect)
            assert Decimal(binding) == Decimal(source)


@pytest.mark.asyncio
async def test_computed_cash_notional_rounding_is_explicit_conservative_and_balanced(state):
    reserve(state)
    await state.oms.submit("owner", "one", Adapter(), lease_token=lease(state))
    fill(state, price=Decimal("10000.000000000000000001"))
    assert state.oms.balances("owner", "paper-account")["wallet"]["USDT"] == Decimal("9899.799999999999999999")
    # An actual fill violating the order limit is preserved as truth and halts.
    assert state.oms.balances("owner", "paper-account")["mode"] == "HALTED"


def supersede_head(state):
    from sqlalchemy import insert

    from qt.persistence.schema import deployment_heads
    with state.repo.transaction() as connection:
        connection.execute(insert(deployment_heads).values(
            tenant_id="owner", account_id="paper-account", deployment_id="new-release",
            status="ACTIVE", generation=2, updated_ns=_NOW, reason="fixture-race",
        ))


def test_obsolete_deployment_cannot_reserve_or_halt_current_account(state):
    supersede_head(state)
    with pytest.raises(Conflict, match="active_account_head"):
        reserve(state)
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"
    assert state.oms.balances("owner", "paper-account")["reserved"]["USDT"] == 0


@pytest.mark.asyncio
async def test_obsolete_deployment_cannot_submit_previously_reserved_intent(state):
    reserve(state)
    supersede_head(state)
    adapter = Adapter()
    with pytest.raises(Conflict, match="active_account_head"):
        await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    assert adapter.submits == 0
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"
    assert state.oms.get_order("owner", "one")["reserved_quote"] == Decimal("100.2")
