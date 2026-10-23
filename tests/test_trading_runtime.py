"""Offline end-to-end Paper/Shadow loop including durable crash recovery."""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from qt.contracts import (
    DeploymentManifest,
    ExecutableMarketSnapshot,
    FeeSchedule,
    InstrumentSpec,
    MarketEvent,
    RiskBudget,
    StrategyArtifact,
    canonical_json,
)
from qt.control.service import ControlService
from qt.execution.oms import OMS
from qt.execution.runtime import TradingRuntime
from qt.execution.simulated import PaperAdapter
from qt.experiments.native import builtin_strategy_code_hash
from qt.marketdata import BookView, PriceLevel
from qt.persistence import Repository, create_engine, migrate
from qt.persistence.schema import accounts, orders
from qt.strategies.native_bridge import NativeEventStrategy

_NOW = 1_700_000_000_000_000_000
_HASH = "a" * 64


@pytest.fixture
def state(tmp_path, monkeypatch):
    now = [_NOW]
    monkeypatch.setattr("qt.execution.oms.time.time_ns", lambda: now[0])
    engine = create_engine("sqlite:///:memory:", allow_sqlite=True)
    migrate(engine)
    repo = Repository(engine)
    oms = OMS(repo)
    oms.create_account("owner", "paper-account", capital=Decimal("10000"))
    service = ControlService(repo, tmp_path, tenant="owner", signing_key=b"s" * 32)
    instrument = InstrumentSpec(
        venue="fixture", instrument="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        settlement_asset="USDT", price_increment="0.01", quantity_increment="0.0001",
        minimum_quantity="0.0001", minimum_notional="0.1", effective_from_ns=0, source="fixture",
    )
    fees = FeeSchedule(venue="fixture", account_tier="fixture", maker_rate="0.001", taker_rate="0.002",
                       fee_basis="notional", fee_asset="USDT", effective_from_ns=0,
                       source="fixture", source_hash=_HASH)
    artifact = StrategyArtifact(
        strategy_id="time_series_momentum", version="1", code_hash=builtin_strategy_code_hash("time_series_momentum"),
        parameters={"lookback": 2}, feature_dependencies=("ohlcv",), instruments=("BTCUSDT",),
        signal_interval_ns=60_000_000_000, signal_ttl_ns=60_000_000_000,
        exit_rules={"mode": "native_strategy"}, risk_budget=RiskBudget(capital="10000", currency="USDT"),
        engine_compatibility=("nautilus",),
    )
    repo.put("owner", "strategy", artifact.model_dump(mode="json"), artifact.content_hash())
    repo.put("owner", "validation", {"experiment_hash": "experiment"}, _HASH)
    repo.put("owner", "experiment_spec", {"strategy_version": artifact.content_hash(),
             "instrument_spec_hashes": [instrument.content_hash()], "engine": {"name": "nautilus"}}, "experiment")
    unsigned = DeploymentManifest(
        deployment_id="test-deployment", strategy_hash=artifact.content_hash(), provider="fixture", model_id="frozen",
        prompt_hash=_HASH, tools_hash=_HASH, risk_policy_hash=service.policy.fingerprint(),
        fee_schedule_hashes=(fees.content_hash(),), validation_report_hash=_HASH,
        venue="fixture", account_id="paper-account", mode="paper",
        risk_budget=RiskBudget(capital="10000", currency="USDT"),
        valid_from_ns=0, valid_until_ns=_NOW + 1_000_000_000_000, signature="pending",
    )

    def save_manifest(mode="paper", **changes):
        data = {**unsigned.model_dump(mode="json", exclude={"signature"}), "mode": mode, **changes}
        signature = hmac.new(service.signing_key, canonical_json(data).encode(), hashlib.sha256).hexdigest()
        service.deployments.activate("owner", DeploymentManifest.model_validate({**data, "signature": signature}))
        return service.verify_deployment(data["deployment_id"])

    def strategy():
        return NativeEventStrategy(artifact, instrument, fees, "test-deployment")

    yield SimpleNamespace(engine=engine, repo=repo, oms=oms, service=service, instrument=instrument,
                          fees=fees, now=now, save_manifest=save_manifest, strategy=strategy)
    engine.dispose()


def market(state, *, bid="101", ask="102", **changes):
    return ExecutableMarketSnapshot(**{
        "venue": "fixture", "instrument": "BTCUSDT", "bid": bid, "ask": ask,
        "asof_ns": state.now[0], "book_valid": True,
        "instrument_spec_hash": state.instrument.content_hash(),
        "fee_schedule_hash": state.fees.content_hash(), **changes,
    })


def event(state, index, close="100"):
    state.now[0] = _NOW + (index + 1) * 60_000_000_000 + 1
    return MarketEvent(
        venue="fixture", instrument="BTCUSDT", event_type="bar",
        exchange_ts_ns=state.now[0] - 1, received_ts_ns=state.now[0],
        available_at_ns=state.now[0] - 1,
        payload={"open_ts_ns": _NOW + index * 60_000_000_000, "close_ts_ns": state.now[0] - 1,
                 "open": "100", "high": "200", "low": "50", "close": close, "volume": "1"},
    )


def book(state, market_value):
    return BookView(last_update_id=1,
                    bids=(PriceLevel(price=market_value.bid, quantity=Decimal("100")),),
                    asks=(PriceLevel(price=market_value.ask, quantity=Decimal("100")),),
                    exchange_ts_ns=state.now[0] - 1, received_ts_ns=state.now[0])


def runtime(state, adapter=None, **changes):
    return TradingRuntime(state.service, state.oms, state.strategy(), "test-deployment",
                          state.instrument, state.fees,
                          adapter or PaperAdapter(state.repo, tenant_id="owner"),
                          clock=lambda: state.now[0], **changes)


async def warmup(state, actor):
    for index in range(3):
        item = event(state, index)
        snapshot = market(state)
        result = await actor.process(item, snapshot, book=book(state, snapshot))
        assert result["status"] == "no_signal", result


@pytest.mark.asyncio
async def test_paper_signal_to_actual_oms_fill_and_reconciled_wallet(state):
    state.save_manifest()
    actor = runtime(state)
    await warmup(state, actor)
    item = event(state, 3, "102")
    snapshot = market(state)
    entered = await actor.process(item, snapshot, book=book(state, snapshot))
    assert entered["status"] == "processed", entered
    assert entered["orders"][0]["status"] == "FILLED"
    assert state.oms.balances("owner", "paper-account")["wallet"]["BTC"] > 0
    assert (await actor.process(item, snapshot, book=book(state, snapshot)))["status"] == "duplicate"
    assert len(state.oms.get_order("owner", entered["intent_ids"][0])["fills"]) == 1
    item = event(state, 4, "99")
    snapshot = market(state, bid="98", ask="99")
    exited = await actor.process(item, snapshot, book=book(state, snapshot))
    assert exited["orders"][0]["status"] == "FILLED"
    assert state.oms.balances("owner", "paper-account")["wallet"]["BTC"] == 0


@pytest.mark.asyncio
async def test_shadow_persists_unsent_intents_without_any_adapter_calls(state):
    state.save_manifest(mode="shadow")

    class Forbidden:
        execution_mode = "live"

        def __getattr__(self, name):
            raise AssertionError("shadow must not access adapter: " + name)

    actor = runtime(state, Forbidden())
    await warmup(state, actor)
    item = event(state, 3, "102")
    snapshot = market(state)
    result = await actor.process(item, snapshot)
    assert result.get("mode") == "shadow", result
    assert result["orders"][0]["status"] == "RESERVED"
    assert state.oms.balances("owner", "paper-account")["wallet"].get("BTC", 0) == 0


@pytest.mark.asyncio
async def test_checkpoint_before_reservation_crash_recovers_original_intent_once(state, monkeypatch):
    state.save_manifest()
    actor = runtime(state, owner="first-process")
    await warmup(state, actor)
    reserve = state.oms.reserve

    def crash(*args, **kwargs):
        raise RuntimeError("simulated process crash after durable event checkpoint")

    monkeypatch.setattr(state.oms, "reserve", crash)
    item = event(state, 3, "102")
    snapshot = market(state)
    with pytest.raises(RuntimeError, match="process crash"):
        await actor.process(item, snapshot, book=book(state, snapshot))
    monkeypatch.setattr(state.oms, "reserve", reserve)
    with state.repo.transaction() as connection:
        connection.execute(update(accounts).values(lease_until_ns=0))
    recovered = runtime(state, owner="restarted-process")
    result = await recovered.process(item, snapshot, book=book(state, snapshot))
    assert result["status"] == "duplicate", result  # recovery completed the prior checkpoint first
    with state.repo.transaction() as connection:
        rows = connection.execute(select(orders)).mappings().all()
    assert len(rows) == 1 and rows[0]["quantity"] > 0
    assert rows[0]["status"] == "FILLED"


@pytest.mark.asyncio
async def test_stale_invalid_and_future_market_halt_before_strategy_or_adapter(state):
    state.save_manifest()
    actor = runtime(state)
    item = event(state, 0)
    snapshot = market(state, asof_ns=state.now[0] - 2_000_000_000)
    result = await actor.process(item, snapshot)
    assert result["status"] == "halted"
    assert state.oms.balances("owner", "paper-account")["mode"] == "HALTED"
    assert actor.strategy.snapshot_state().values["rows"] == ()


@pytest.mark.asyncio
async def test_wrong_adapter_and_unsigned_manifest_never_execute(state):
    state.save_manifest()
    bad = SimpleNamespace(execution_mode="live")
    actor = runtime(state, bad)
    item = event(state, 0)
    assert (await actor.process(item, market(state)))["status"] == "halted"
    with state.repo.transaction() as connection:
        from qt.persistence.schema import artifacts
        connection.execute(update(artifacts).where(artifacts.c.kind == "deployment").values(
            payload={"signature": "forged"},
        ))
    fresh = runtime(state)
    assert (await fresh.process(item, market(state)))["status"] == "blocked"


@pytest.mark.asyncio
async def test_competing_actor_cannot_mutate_strategy_or_send(state):
    state.save_manifest()
    actor = runtime(state, owner="primary")
    await warmup(state, actor)
    competing = runtime(state, owner="second")
    item = event(state, 3, "102")
    result = await competing.process(item, market(state))
    assert result["status"] == "blocked" and "lease" in result["reason"]
    assert competing.strategy.snapshot_state().values["rows"] == ()
