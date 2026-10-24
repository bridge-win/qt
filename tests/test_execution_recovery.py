"""Adversarial lease, snapshot, settlement and never-sent recovery paths."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from qt.contracts import OrderIdentity, OrderSnapshot
from qt.execution.oms import OMS
from qt.persistence import Conflict
from qt.persistence.schema import accounts, artifacts, events, fills, runtime_health
from tests.test_execution_oms import Adapter, book_adapter, fill, lease, reserve, supersede_head
from tests.test_execution_oms import state as state

_NOW = 1_700_000_000_000_000_000


def test_owner_name_alone_does_not_renew_another_process_lease(state):
    token = lease(state)
    restarted = OMS(state.repo)
    with pytest.raises(Conflict, match="lease_held"):
        restarted.claim_account("owner", "paper-account", owner="executor")
    with pytest.raises(Conflict, match="lease_held"):
        restarted.claim_account("owner", "paper-account", owner="executor", lease_token="wrong")
    assert restarted.claim_account("owner", "paper-account", owner="executor", lease_token=token) == token
    assert lease(state) == token


def test_lease_takeover_samples_clock_after_the_account_lock(state, monkeypatch):
    first = lease(state)
    original = state.oms._account

    def waited(connection, tenant, account_id):
        account = original(connection, tenant, account_id)
        monkeypatch.setattr("time.time_ns", lambda: _NOW + 31_000_000_000)
        return account

    monkeypatch.setattr(state.oms, "_account", waited)
    replacement = state.oms.claim_account("owner", "paper-account", owner="replacement")
    assert replacement != first
    with state.repo.transaction() as connection:
        account = connection.execute(select(accounts)).mappings().one()
    assert account["lease_until_ns"] == _NOW + 61_000_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", ["0.01", "0.004"])
async def test_late_submission_timeout_cannot_undo_confirmed_fill_or_halt_account(state, quantity):
    reserve(state)
    adapter = Adapter()
    adapter.during_submit = lambda _: fill(state, quantity=Decimal(quantity), fee=Decimal(quantity) * 20)
    adapter.exception = TimeoutError("late acknowledgement after account-stream fill")
    result = await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    assert result["status"] == ("FILLED" if quantity == "0.01" else "PARTIALLY_FILLED")
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"
    assert len(state.oms.get_order("owner", "one")["fills"]) == 1


@pytest.mark.asyncio
async def test_older_order_snapshot_cannot_replace_newer_reconciliation(state, monkeypatch):
    reserve(state)
    adapter = Adapter()
    await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    monkeypatch.setattr("time.time_ns", lambda: _NOW + 10)
    identity = OrderIdentity(venue="fixture", account_id="paper-account", intent_id="one", venue_order_id="venue-one")
    adapter.snapshot = OrderSnapshot(identity=identity, status="ACKNOWLEDGED", quantity="0.01",
                                     filled_quantity="0", asof_ns=_NOW + 10)
    await state.oms.query("owner", "one", adapter)
    adapter.snapshot = adapter.snapshot.model_copy(update={"status": "CANCELED", "asof_ns": _NOW})
    result = await state.oms.query("owner", "one", adapter)
    assert result["snapshot_ignored"] and result["status"] == "ACKNOWLEDGED"
    assert state.oms.balances("owner", "paper-account")["reserved"]["USDT"] == Decimal("100.2")
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"


@pytest.mark.asyncio
async def test_open_order_snapshot_does_not_confirm_pending_cancel(state):
    reserve(state)
    adapter = Adapter(cancel="unknown")
    token = lease(state)
    await state.oms.submit("owner", "one", adapter, lease_token=token)
    await state.oms.cancel("owner", "one", adapter, lease_token=token)
    adapter.snapshot = OrderSnapshot(identity=OrderIdentity(venue="fixture", account_id="paper-account",
        intent_id="one", venue_order_id="venue-one"), status="ACKNOWLEDGED", quantity="0.01",
        filled_quantity="0", asof_ns=_NOW)
    result = await state.oms.query("owner", "one", adapter)
    assert result["status"] == "CANCEL_PENDING" and result["snapshot_ignored"]
    with pytest.raises(ValueError, match="unresolved_execution"):
        state.oms.mark_reconciled("owner", "paper-account")


@pytest.mark.asyncio
async def test_settlement_reorg_of_duplicate_fill_is_recorded_once_without_repeating_trade(state):
    reserve(state)
    adapter = Adapter()
    await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    fill(state, settlement_status="PENDING")
    wallet = state.oms.balances("owner", "paper-account")["wallet"]
    fill(state, settlement_status="REORGED")
    fill(state)  # A replay without settlement metadata cannot erase the reorg.
    order = state.oms.get_order("owner", "one")
    assert len(order["fills"]) == 1 and order["settlement_status"] == "REORGED"
    assert state.oms.balances("owner", "paper-account")["wallet"] == wallet
    assert state.oms.balances("owner", "paper-account")["mode"] == "HALTED"
    with state.repo.transaction() as connection:
        assert connection.execute(select(func.count()).select_from(fills)).scalar_one() == 1
    with pytest.raises(ValueError, match="unresolved_settlement"):
        state.oms.mark_reconciled("owner", "paper-account")
    with pytest.raises(ValueError, match="unresolved_settlement"):
        state.oms.set_mode("owner", "paper-account", "ACTIVE", "cannot bypass unresolved chain truth")


@pytest.mark.asyncio
async def test_pending_settlement_blocks_new_risk_until_authoritative_confirmation(state):
    reserve(state)
    adapter = Adapter()
    await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    fill(state, settlement_status="PENDING")
    with pytest.raises(ValueError, match="settlement_requires_reconciliation"):
        reserve(state, "pending-is-not-spendable")
    adapter.snapshot = OrderSnapshot(identity=OrderIdentity(venue="fixture", account_id="paper-account",
        intent_id="one", venue_order_id="venue-one"), status="FILLED", settlement_status="CONFIRMED",
        quantity="0.01", filled_quantity="0.01", asof_ns=_NOW)
    await state.oms.query("owner", "one", adapter)
    state.oms.mark_reconciled("owner", "paper-account")
    state.oms.set_mode("owner", "paper-account", "ACTIVE", "verified settlement and reviewed recovery")
    assert reserve(state, "now-confirmed")["status"] == "RESERVED"


@pytest.mark.asyncio
async def test_fee_rounding_to_supported_decimal_unit_does_not_falsely_halt(state):
    reserve(state)
    await state.oms.submit("owner", "one", Adapter(), lease_token=lease(state))
    fill(state, quantity=Decimal("0.0001"), price=Decimal("9999.000000000000000001"),
         fee=Decimal("0.001999800000000001"))
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"


@pytest.mark.asyncio
async def test_shadow_hundreds_of_approved_intents_never_send_or_leak_reservations(state):
    state.deployment = state.deployment.model_copy(update={"mode": "shadow"})
    token = lease(state)

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("shadow cannot access an execution adapter")

    for number in range(350):
        identity = f"shadow-{number}"
        reserve(state, identity)
        result = await state.oms.submit("owner", identity, Forbidden(), lease_token=token)
        assert result["status"] == "CANCELED" and result["sent"] is False and result["shadow"]
    balance = state.oms.balances("owner", "paper-account")
    assert balance["mode"] == "ACTIVE" and balance["reserved"]["USDT"] == 0
    assert balance["wallet"] == {"USDT": Decimal("10000")}
    with state.repo.transaction() as connection:
        assert connection.execute(select(accounts.c.daily_new_risk)).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(events).where(
            events.c.event_type == "shadow_intent_evaluated",
        )).scalar_one() == 350


@pytest.mark.asyncio
async def test_paper_partial_ioc_terminal_snapshot_survives_later_fill_ingestion_time(state, monkeypatch):
    clock = {"now": _NOW}

    def advancing():
        clock["now"] += 1
        return clock["now"]

    monkeypatch.setattr("time.time_ns", advancing)
    reserve(state, quantity="0.01", limit_price="10001", time_in_force="IOC")
    adapter = book_adapter(state, asks=[("10001", "0.004")])
    await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    await state.oms.reconcile("owner", "paper-account", adapter, venue="fixture")
    order = state.oms.get_order("owner", "one")
    assert order["status"] == "CANCELED" and order["filled_quantity"] == Decimal("0.004")
    assert state.oms.balances("owner", "paper-account")["reserved"]["USDT"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["halted", "reduce_only", "book_invalid"])
async def test_last_local_preflight_blocks_intervening_risk_or_data_failure_without_network_call(state, monkeypatch, failure):
    reserve(state)
    token = lease(state)
    state.repo.health("owner", "market:fixture:BTCUSDT", state.market.model_dump(mode="json"))
    original, calls = state.oms._account, {"count": 0}

    def intervening(connection, tenant, account_id):
        account = original(connection, tenant, account_id)
        calls["count"] += 1
        if calls["count"] == 2:  # After SUBMITTING commit, before the actual adapter call.
            if failure == "book_invalid":
                connection.execute(update(runtime_health).where(runtime_health.c.name == "market:fixture:BTCUSDT").values(
                    payload={"book_valid": False, "reason": "sequence_gap"},
                ))
            else:
                account["mode"] = failure.upper()
                connection.execute(update(accounts).where(accounts.c.account_id == account_id).values(mode=account["mode"]))
        return account

    monkeypatch.setattr(state.oms, "_account", intervening)
    adapter = Adapter()
    result = await state.oms.submit("owner", "one", adapter, lease_token=token)
    assert result["status"] == "CANCELED" and result["never_sent"] is True
    assert adapter.submits == 0
    assert state.oms.balances("owner", "paper-account")["reserved"]["USDT"] == 0
    assert state.oms.balances("owner", "paper-account")["mode"] == (
        "REDUCE_ONLY" if failure == "reduce_only" else "HALTED")
    with state.repo.transaction() as connection:
        assert connection.execute(select(events.c.payload).where(
            events.c.event_type == "order_aborted_before_send",
        )).scalar_one()["never_sent"] is True


@pytest.mark.asyncio
async def test_paper_depth_history_tampering_is_detected_before_another_fill(state):
    from qt.contracts import ReconcileCursor

    adapter = book_adapter(state)
    reserve(state, limit_price="10001")
    await state.oms.submit("owner", "one", adapter, lease_token=lease(state))
    with state.repo.transaction() as connection:
        row = connection.execute(select(artifacts).where(artifacts.c.kind == "paper_order_result")).mappings().one()
        forged = {**row["payload"], "fills": []}
        connection.execute(update(artifacts).where(artifacts.c.kind == "paper_order_result").values(payload=forged))
    with pytest.raises(Conflict, match="paper_result_integrity_failure"):
        await adapter.reconcile(ReconcileCursor(venue="fixture", account_id="paper-account"))


@pytest.mark.asyncio
async def test_fill_cannot_fabricate_a_trade_for_a_locally_canceled_unsent_order(state):
    reserve(state)
    adapter = Adapter()
    await state.oms.cancel("owner", "one", adapter, lease_token=lease(state))
    with pytest.raises(ValueError, match="proven_never_sent"):
        fill(state)
    assert state.oms.balances("owner", "paper-account")["wallet"] == {"USDT": Decimal("10000")}


def test_obsolete_deployment_cannot_change_the_new_account_heads_mode(state):
    supersede_head(state)
    with pytest.raises(Conflict, match="active_account_head"):
        state.oms.set_mode("owner", "paper-account", "HALTED", "stale control request",
                           deployment_id="test-deployment")
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"
