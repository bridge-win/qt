"""Historical releases never retain authority over a new active account head."""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from types import SimpleNamespace

import pytest

from qt.contracts import (
    DeploymentManifest,
    ExecutableMarketSnapshot,
    FeeSchedule,
    InstrumentSpec,
    OrderIntent,
    RiskBudget,
    canonical_json,
)
from qt.control.service import ControlService
from qt.execution.oms import OMS
from qt.operations.watchdog import Watchdog
from qt.persistence import Conflict, Repository, create_engine, migrate

_HASH = "a" * 64
_NOW = 1_700_000_000_000_000_000


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    now = [_NOW]
    monkeypatch.setattr("qt.operations.releases.time.time_ns", lambda: now[0])
    engine = create_engine("sqlite:///:memory:", allow_sqlite=True)
    migrate(engine)
    repo = Repository(engine)
    service = ControlService(repo, tmp_path, tenant="owner", signing_key=b"l" * 32)
    oms = OMS(repo)
    oms.create_account("owner", "account", capital=Decimal("10000"))
    instrument = InstrumentSpec(venue="fixture", instrument="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        settlement_asset="USDT", price_increment="0.01", quantity_increment="0.001",
        minimum_quantity="0.001", minimum_notional="1", effective_from_ns=0, source="fixture")
    fee = FeeSchedule(venue="fixture", account_tier="test", maker_rate="0", taker_rate="0",
        fee_basis="notional", fee_asset="USDT", effective_from_ns=0, source="fixture", source_hash=_HASH)
    repo.put("owner", "instrument", instrument.model_dump(mode="json"), instrument.content_hash())
    repo.put("owner", "validation", {"experiment_hash": "test"}, _HASH)
    repo.put("owner", "experiment_spec", {"instrument_spec_hashes": [instrument.content_hash()]}, "test")

    def signed(identity, **changes):
        manifest = DeploymentManifest(**{
            "deployment_id": identity, "strategy_hash": _HASH, "provider": "rules", "model_id": "frozen",
            "prompt_hash": _HASH, "tools_hash": _HASH, "risk_policy_hash": service.policy.fingerprint(),
            "fee_schedule_hashes": (fee.content_hash(),), "validation_report_hash": _HASH,
            "venue": "fixture", "account_id": "account", "mode": "paper",
            "risk_budget": RiskBudget(capital="10000", currency="USDT"),
            "valid_from_ns": now[0] - 1, "valid_until_ns": now[0] + 1_000_000_000,
            "signature": "pending", **changes,
        })
        data = manifest.model_dump(mode="json", exclude={"signature"})
        signature = hmac.new(service.signing_key, canonical_json(data).encode(), hashlib.sha256).hexdigest()
        return DeploymentManifest.model_validate({**data, "signature": signature})

    yield SimpleNamespace(repo=repo, service=service, oms=oms, now=now, signed=signed,
                          instrument=instrument, fee=fee)
    engine.dispose()


def test_expired_historical_version_cannot_halt_or_verify_over_new_active_version(lifecycle):
    state = lifecycle
    first = state.signed("v1")
    state.service.deployments.activate("owner", first)
    assert state.service.verify_deployment("v1") == first
    state.now[0] += 2_000_000_000
    with pytest.raises(Conflict):
        state.service.verify_deployment("v1")
    state.service.deactivate_deployment("v1", reason="expired flat release")
    second = state.signed("v2")
    state.service.deployments.activate("owner", second)
    assert [row["deployment_id"] for row in state.service.active_deployments()] == ["v2"]
    assert state.service.verify_deployment("v2") == second
    watchdog = Watchdog(state.service, now_ns=lambda: state.now[0])
    assert watchdog.check_deployment("v1")["status"] == "inactive"
    report = watchdog.check()
    assert [item["deployment_id"] for item in report["deployments"]] == ["v2"]
    assert state.oms.balances("owner", "account")["mode"] == "ACTIVE"
    assert state.service.deployments.read_signed("owner", "v1") == first


def test_replacement_is_atomic_idempotent_and_old_identity_cannot_reactivate(lifecycle):
    state = lifecycle
    first, second = state.signed("v1"), state.signed("v2")
    original = state.service.deployments.activate("owner", first)
    assert state.service.deployments.activate("owner", first) == original
    state.service.deployments.activate("owner", second)
    assert not state.service.is_active_deployment("v1")
    with pytest.raises(Conflict, match="active_account_head"):
        state.service.verify_deployment("v1")
    with pytest.raises(Conflict, match="historical"):
        state.service.deployments.activate("owner", first)
    assert state.service.verify_deployment("v2") == second


def test_new_release_and_deactivation_require_flat_actual_inventory(lifecycle):
    state = lifecycle
    state.service.deployments.activate("owner", state.signed("v1"))
    with state.repo.transaction() as connection:
        account = state.oms._account(connection, "owner", "account")
        state.oms._journal(connection, account, "fixture_fill", "trade-1", [
            ("BTC", "wallet", Decimal("1")), ("BTC", "counterparty", Decimal("-1")),
            ("USDT", "wallet", Decimal("-100")), ("USDT", "counterparty", Decimal("100")),
        ], state.now[0])
    with pytest.raises(Conflict, match="flat_inventory"):
        state.service.deployments.activate("owner", state.signed("v2"))
    with pytest.raises(Conflict, match="flat_inventory"):
        state.service.deactivate_deployment("v1", reason="cannot abandon real risk")
    assert state.service.is_active_deployment("v1")
    with pytest.raises(KeyError):
        state.repo.get("owner", "deployment", "v2")


def test_pending_orders_and_unknown_effects_block_replacement(lifecycle):
    state = lifecycle
    first = state.signed("v1")
    state.service.deployments.activate("owner", first)
    market = ExecutableMarketSnapshot(venue="fixture", instrument="BTCUSDT", bid="99", ask="100",
        asof_ns=state.now[0], book_valid=True, instrument_spec_hash=state.instrument.content_hash(),
        fee_schedule_hash=state.fee.content_hash())
    intent = OrderIntent(intent_id="order", account_id="account", deployment_id="v1", signal_id="signal",
        venue="fixture", instrument="BTCUSDT", side="buy", quantity="1", limit_price="100", time_in_force="IOC",
        strategy_hash=_HASH, policy_hash=state.service.policy.fingerprint(), fee_schedule_hash=state.fee.content_hash(),
        instrument_spec_hash=state.instrument.content_hash(), risk_reservation_id="reservation",
        created_at_ns=state.now[0], valid_until_ns=state.now[0] + 100_000_000)
    state.oms.reserve("owner", intent, instrument=state.instrument, fees=state.fee,
                      deployment=first, market=market)
    with pytest.raises(Conflict, match="no_orders"):
        state.service.deployments.activate("owner", state.signed("v2"))
    with pytest.raises(Conflict, match="no_orders"):
        state.service.deactivate_deployment("v1", reason="pending order still consumes risk")


def test_invalid_signatures_missing_heads_and_risk_escalation_fail_closed(lifecycle):
    state = lifecycle
    first = state.signed("v1")
    state.repo.put("owner", "deployment", first.model_dump(mode="json"), "v1")
    with pytest.raises(Conflict, match="active_account_head"):
        state.service.verify_deployment("v1")
    with pytest.raises(Conflict, match="signature"):
        state.service.deployments.activate("owner", state.signed("bad").model_copy(update={"signature": "forged"}))
    # A bare immutable record is historical, not implicit authority to execute.
    active = state.signed("active")
    state.service.deployments.activate("owner", active)
    increased = state.signed("larger", risk_budget=RiskBudget(capital="10000", currency="USDT",
                                                            maximum_position_fraction="0.5"))
    with pytest.raises(Conflict, match="risk_increase"):
        state.service.deployments.activate("owner", increased)
    assert state.service.verify_deployment("active") == active


def test_inactive_head_must_be_explicitly_released_and_history_retained(lifecycle):
    state = lifecycle
    first = state.signed("v1")
    state.service.deployments.activate("owner", first)
    state.service.deactivate_deployment("v1", reason="operator deactivation")
    assert not state.service.active_deployments()
    with pytest.raises(Conflict, match="active_account_head"):
        state.service.verify_deployment("v1")
    assert state.repo.get("owner", "deployment", "v1")["payload"] == first.model_dump(mode="json")
    with pytest.raises(Conflict, match="historical"):
        state.service.deployments.activate("owner", first)
