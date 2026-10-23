"""Loss and infrastructure stops commit before refusing any additional risk."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update

from qt.operations.risk import RiskBudgetExceededError, RiskGuard, StaleMarketObservationError
from qt.operations.watchdog import Watchdog
from qt.persistence import Conflict
from qt.persistence.schema import accounts, portfolio_snapshots, risk_state, runtime_health
from tests.test_execution_oms import Adapter, fill, intent, lease, reserve
from tests.test_execution_oms import state as state

_NOW = 1_700_000_000_000_000_000


def market(state, bid):
    value = Decimal(bid)
    return state.market.model_copy(update={"bid": value, "ask": value + 1})


def publish_healthy(state):
    state.repo.put("owner", "deployment", state.deployment.model_dump(mode="json"), "test-deployment")
    state.repo.put("owner", "validation", {"experiment_hash": "experiment"}, state.deployment.validation_report_hash)
    state.repo.put("owner", "experiment_spec", {"instrument_spec_hashes": [state.instrument.content_hash()]}, "experiment")
    state.repo.put("owner", "instrument", state.instrument.model_dump(mode="json"), state.instrument.content_hash())
    state.repo.health("owner", "market:fixture:BTCUSDT", state.market.model_dump(mode="json"))
    state.repo.health("owner", "trading:test-deployment", {"healthy": True})
    lease(state)
    service = SimpleNamespace(repository=state.repo, tenant="owner", verify_deployment=lambda _: state.deployment,
                              active_deployments=lambda: ["test-deployment"])
    return Watchdog(service, now_ns=lambda: _NOW)


@pytest.mark.asyncio
async def test_daily_loss_latches_before_rejected_buy_and_preserves_protective_exit(state):
    reserve(state, quantity="0.02")
    token = lease(state)
    await state.oms.submit("owner", "one", Adapter(), lease_token=token)
    fill(state, quantity=Decimal("0.02"), fee=Decimal("0.4"))
    state.market = market(state, "7000")
    with pytest.raises(RiskBudgetExceededError, match="maximum_daily_loss"):
        reserve(state, "must-not-open")
    assert state.oms.balances("owner", "paper-account")["mode"] == "REDUCE_ONLY"
    with state.repo.transaction() as connection:
        risk = connection.execute(select(risk_state)).mappings().one()
        assert risk["last_equity"] == Decimal("9939.6")
        assert "maximum_daily_loss" in risk["latched_reasons"]
        connection.execute(update(accounts).values(daily_new_risk=Decimal("10000")))
    # The entry budget is exhausted; a genuine owned-inventory exit still works.
    reserve(state, "exit", side="sell", quantity="0.02", limit_price="7000", reduce_only=True)
    await state.oms.submit("owner", "exit", Adapter(), lease_token=token)
    fill(state, "exit", "exit-fill", quantity=Decimal("0.02"), price=Decimal("7000"), fee=Decimal("0.28"))
    assert state.oms.balances("owner", "paper-account")["wallet"]["BTC"] == 0


@pytest.mark.asyncio
async def test_drawdown_uses_high_water_and_fixed_capital_even_when_still_profitable(state):
    reserve(state)
    await state.oms.submit("owner", "one", Adapter(), lease_token=lease(state))
    fill(state)
    guard = RiskGuard(state.repo)
    high = guard.check("owner", state.deployment, state.instrument, market(state, "100000"))
    assert high["high_water_equity"] == Decimal("10899.8")
    dropped = guard.check("owner", state.deployment, state.instrument, market(state, "60000"))
    assert dropped["equity"] == Decimal("10499.8")
    assert dropped["drawdown"] == Decimal("400")
    assert dropped["drawdown_limit"] == Decimal("300")
    assert dropped["mode"] == "REDUCE_ONLY"
    assert "maximum_drawdown" in dropped["reasons"]
    recovered = guard.check("owner", state.deployment, state.instrument, market(state, "110000"))
    assert recovered["mode"] == "REDUCE_ONLY"
    assert not recovered["safe_new_risk"]


def test_guard_uses_actual_wallet_and_persists_snapshots_not_order_quantity(state):
    reserve(state)
    report = RiskGuard(state.repo).check("owner", state.deployment, state.instrument, state.market)
    assert report["equity"] == Decimal("10000")
    assert report["wallet"].get("BTC", 0) == 0
    with state.repo.transaction() as connection:
        snapshots = connection.execute(select(portfolio_snapshots)).mappings().all()
        assert snapshots
        assert snapshots[-1]["payload"]["wallet"] == {"USDT": "10000"}


def test_guard_rejects_changed_policy_without_resetting_high_water(state):
    reserve(state)
    changed = state.deployment.model_copy(update={"risk_policy_hash": "b" * 64})
    report = RiskGuard(state.repo).check("owner", changed, state.instrument, state.market)
    assert report["mode"] == "HALTED"
    assert report["reasons"] == ["risk_policy_changed_without_review"]
    with state.repo.transaction() as connection:
        assert connection.execute(select(risk_state.c.policy_hash)).scalar_one() == "a" * 64


def test_watchdog_checks_all_signed_deployments_and_healthy_lease_market(state):
    watch = publish_healthy(state)
    assert watch.check()["status"] == "healthy"
    result = watch.check_deployment("test-deployment")
    assert result["mode"] == "ACTIVE" and result["equity"] == Decimal("10000")


@pytest.mark.parametrize("failure, expected", [
    ("book_invalid", "untrusted_or_stale_market"),
    ("market_old", "market_health_missing_or_stale"),
    ("heartbeat", "trading_runtime_heartbeat_missing_or_unhealthy"),
    ("lease", "execution_lease_lost"),
    ("account_difference", "account_reconciliation_difference"),
])
def test_independent_watchdog_latches_stops_and_does_not_auto_resume(state, failure, expected):
    watch = publish_healthy(state)
    if failure == "book_invalid":
        state.repo.health("owner", "market:fixture:BTCUSDT", state.market.model_copy(update={"book_valid": False}).model_dump(mode="json"))
    elif failure == "market_old":
        with state.repo.transaction() as connection:
            connection.execute(update(runtime_health).where(runtime_health.c.name == "market:fixture:BTCUSDT").values(observed_ns=_NOW - 2_000_000_000))
    elif failure == "heartbeat":
        state.repo.health("owner", "trading:test-deployment", {"healthy": False})
    elif failure == "lease":
        with state.repo.transaction() as connection:
            connection.execute(update(accounts).values(lease_until_ns=0))
    else:
        state.repo.health("owner", "account:paper-account", {"reconciled": True, "differences": {"USDT": "0.01"}})
    result = watch.check_deployment("test-deployment")
    assert result["status"] == "stopped" and result["mode"] == "HALTED"
    assert expected in result["reasons"]
    state.repo.health("owner", "market:fixture:BTCUSDT", state.market.model_dump(mode="json"))
    state.repo.health("owner", "trading:test-deployment", {"healthy": True})
    state.repo.health("owner", "account:paper-account", {"reconciled": True, "differences": {"USDT": "0"}})
    lease(state)
    assert watch.check_deployment("test-deployment")["mode"] == "HALTED"


def test_invalid_deployment_signature_cannot_keep_risk_enabled(state):
    watch = publish_healthy(state)
    def invalid(_):
        raise Conflict("signature failed")
    watch.service.verify_deployment = invalid
    result = watch.check_deployment("test-deployment")
    assert result["mode"] == "HALTED"
    assert result["reasons"] == ["deployment_or_instrument_verification_failed"]


def test_watchdog_database_failure_reports_unpersisted_stop_without_exposing_credentials(state, monkeypatch):
    watch = publish_healthy(state)
    def unavailable():
        raise RuntimeError("postgresql://sensitive:secret@private")
    monkeypatch.setattr(state.repo, "transaction", unavailable)
    result = watch.check()
    assert result["status"] == "unavailable"
    assert result["mode"] == "HALTED" and result["persisted"] is False
    assert "secret" not in str(result) and "private" not in str(result)


def test_historical_deployment_never_halts_another_active_version(state):
    watch = publish_healthy(state)
    watch.service.active_deployments = lambda: ["new-deployment"]
    result = watch.check_deployment("test-deployment")
    assert result["status"] == "inactive" and result["persisted"] is False
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"


def test_initial_warmup_without_risk_does_not_latch_a_stop(state):
    watch = publish_healthy(state)
    with state.repo.transaction() as connection:
        from sqlalchemy import delete
        connection.execute(delete(runtime_health))
    result = watch.check_deployment("test-deployment")
    assert result["status"] == "awaiting_runtime"
    assert result["safe_new_risk"] is False and result["persisted"] is False
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"


@pytest.mark.asyncio
async def test_current_data_cannot_regress_persisted_risk_time(state):
    reserve(state)
    old_market = state.market.model_copy(update={"asof_ns": _NOW - 1})
    result = RiskGuard(state.repo).check("owner", state.deployment, state.instrument, old_market, now_ns=_NOW - 1)
    assert result["mode"] == "HALTED"
    assert "risk_mark_time_regression" in result["reasons"]


def test_out_of_order_market_snapshot_is_discarded_without_halting_account(state):
    reserve(state)
    with state.repo.transaction() as connection:
        before = dict(connection.execute(select(risk_state)).mappings().one())
    old_market = state.market.model_copy(update={"asof_ns": _NOW - 1})
    result = RiskGuard(state.repo).check("owner", state.deployment, state.instrument, old_market)
    assert result["observation_discarded"] and result["persisted"] is False
    assert result["mode"] == "ACTIVE" and not result["safe_new_risk"]
    state.market = old_market
    with pytest.raises(StaleMarketObservationError, match="superseded_market_observation"):
        reserve(state, "must-use-current-market")
    with state.repo.transaction() as connection:
        assert dict(connection.execute(select(risk_state)).mappings().one()) == before
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"


@pytest.mark.asyncio
async def test_price_appreciation_can_breach_position_limit_without_new_orders(state):
    reserve(state)
    await state.oms.submit("owner", "one", Adapter(), lease_token=lease(state))
    fill(state)
    report = RiskGuard(state.repo).check("owner", state.deployment, state.instrument, market(state, "400000"))
    assert report["mode"] == "REDUCE_ONLY"
    assert "maximum_position_exposure" in report["reasons"]


@pytest.mark.asyncio
async def test_valuation_floors_subunit_precision_instead_of_database_rounding(state):
    reserve(state)
    await state.oms.submit("owner", "one", Adapter(), lease_token=lease(state))
    fill(state)
    report = RiskGuard(state.repo).check("owner", state.deployment, state.instrument,
                                         market(state, "10000.000000000000000001"))
    assert report["equity"] == Decimal("9999.8")
    assert report["valuation_rounding"] == "floor_to_18_decimal_places"


def test_risk_clock_is_sampled_after_waiting_for_the_account_lock(state, monkeypatch):
    guard = RiskGuard(state.repo)
    guard.check("owner", state.deployment, state.instrument, state.market, now_ns=_NOW + 50)
    clock = {"now": _NOW}
    monkeypatch.setattr("time.time_ns", lambda: clock["now"])

    def wait_for_account_lock(connection, cursor, statement, parameters, context, executemany):
        if "FROM" in statement and "accounts" in statement:
            clock["now"] = _NOW + 100

    event.listen(state.repo.engine, "before_cursor_execute", wait_for_account_lock)
    try:
        result = guard.check("owner", state.deployment, state.instrument, state.market)
    finally:
        event.remove(state.repo.engine, "before_cursor_execute", wait_for_account_lock)
    assert result["safe_new_risk"] and result["mode"] == "ACTIVE"
    # Explicit historical/replay clocks must still reject backwards marks.
    assert "risk_mark_time_regression" in guard.check(
        "owner", state.deployment, state.instrument, state.market, now_ns=_NOW,
    )["reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reserve", "submit"])
async def test_oms_samples_risk_clock_after_own_account_lock(state, monkeypatch, operation):
    if operation == "submit":
        reserve(state)
    token = lease(state)
    state.oms.risk_guard.check("owner", state.deployment, state.instrument, state.market, now_ns=_NOW + 50)
    clock = {"now": _NOW}
    monkeypatch.setattr("time.time_ns", lambda: clock["now"])
    original = state.oms._account

    def after_wait(connection, tenant, account_id):
        account = original(connection, tenant, account_id)
        clock["now"] = _NOW + 100
        return account

    monkeypatch.setattr(state.oms, "_account", after_wait)
    if operation == "reserve":
        result = state.oms._reserve("owner", intent(state), instrument=state.instrument,
                                    fees=state.fees, deployment=state.deployment, market=state.market)
        assert result["status"] == "RESERVED"
    else:
        result = await state.oms._submit("owner", "one", Adapter(), lease_token=token,
                                         checked_market=state.market)
        assert result["status"] == "ACKNOWLEDGED"
    assert state.oms.balances("owner", "paper-account")["mode"] == "ACTIVE"
