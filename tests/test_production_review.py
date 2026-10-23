"""Adversarial regression cases from independent production-boundary review.

No browser, paid model calls, external account or order adapter is used. Trusted
acceptance records below simulate legitimately collected forward evidence; an
ordinary HTTP caller has no endpoint for writing those records.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import pyarrow as pa
import pytest
from sqlalchemy import select, update

from qt.contracts import content_hash
from qt.control.service import ControlService
from qt.operations.budgets import UsageLedger
from qt.operations.gates import FORWARD, INTEGRITY, STRATEGY, GatePolicy
from qt.persistence import Conflict, Repository, create_engine, migrate
from qt.persistence.schema import artifacts, jobs, model_calls, usage_budgets


@pytest.fixture
def service(tmp_path):
    engine = create_engine("sqlite:///:memory:", allow_sqlite=True)
    migrate(engine)
    control = ControlService(Repository(engine), tmp_path / "datasets", tenant="review",
                             signing_key=b"production-review-test-signing-key-only")
    yield control
    engine.dispose()


def _case(service, *, campaign_dataset=None, suffix="a", venue="binance", instrument_type="spot"):
    manifest = service.datasets.register_table(
        pa.table({"timestamp_ns": list(range(10, 101, 10)),
                  "open": [100.0 if suffix == "a" else 200.0] * 10,
                  "high": [101.0 if suffix == "a" else 201.0] * 10,
                  "low": [99.0 if suffix == "a" else 199.0] * 10,
                  "close": [100.0 if suffix == "a" else 200.0] * 10,
                  "volume": [1.0] * 10}),
        venue=venue, instrument="BTCUSDT", data_type="ohlcv",
        timestamp_column="timestamp_ns", rights="synthetic_test_only",
        provenance={"timestamp_semantics": "close", "fixture": suffix},
        availability="reconstructed_available", expected_interval_ns=10,
    )
    service.register("dataset", manifest.model_dump(mode="json"))
    instrument = service.register("instrument", {
        "venue": venue, "instrument": "BTCUSDT", "base_asset": "BTC",
        "quote_asset": "USDT", "settlement_asset": "USDT", "price_increment": "0.01",
        "quantity_increment": "0.00001", "minimum_quantity": "0.00001",
        "minimum_notional": "1", "effective_from_ns": 0, "source": "review_fixture",
        "instrument_type": instrument_type,
    })
    fee = service.register("fees", {
        "venue": venue, "account_tier": "review", "maker_rate": "0.001",
        "taker_rate": "0.001", "fee_basis": "notional", "fee_asset": "USDT",
        "effective_from_ns": 0, "source": "review_fixture", "source_hash": "a" * 64,
    })
    strategy = service.register("strategy", {
        "strategy_id": "sma_crossover", "version": "review-v1", "code_hash": "a" * 64,
        "parameters": {"kind": "sma_crossover", "fast": 3, "slow": 8},
        "feature_dependencies": ["close"], "instruments": ["BTCUSDT"],
        "signal_interval_ns": 10, "signal_ttl_ns": 10, "exit_rules": {},
        "risk_budget": {"capital": "10000", "currency": "USDT"},
        "engine_compatibility": ["nautilus"],
    })
    campaign_id = "campaign-" + suffix
    service.experiments.register_campaign(
        service.tenant, campaign_id, dataset_id=campaign_dataset or manifest.dataset_id,
        holdout_start_ns=80, holdout_end_ns=110, protocol={"primary": "net_return"},
    )
    spec = {
        "campaign_id": campaign_id, "strategy_version": strategy["entity_id"],
        "dataset_ids": [manifest.dataset_id], "instruments": ["BTCUSDT"],
        "instrument_spec_hashes": [instrument["entity_id"]], "timeframes_ns": [10],
        "split": {"method": "walk_forward", "training_start_ns": 10,
                  "training_end_ns": 20, "validation_end_ns": 30, "test_end_ns": 70,
                  "label_information_interval": "closed_bar_to_next_close"},
        "cost_model": {"version": "review-v1", "fee_schedule_hashes": [fee["entity_id"]], "slippage_bps": "0"},
        "fill_model": {"name": "native_bar", "version": "rc4-v1"},
        "latency_model": {"version": "review-v1", "feed_ns": 0, "decision_ns": 0, "submission_ns": 0, "exchange_ns": 0},
        "benchmarks": ["cash"], "seed": 7,
        "engine": {"name": "nautilus", "version": "2.0.0rc4", "image_digest": "sha256:" + "b" * 64},
        "policy_version": service.policy.version,
    }
    return spec, strategy, fee, manifest


def test_campaign_cannot_account_for_different_dataset(service):
    _case(service)
    spec, _, _, _ = _case(service, suffix="b")
    spec["campaign_id"] = "campaign-a"
    with pytest.raises((ValueError, Conflict)):
        service.create_experiment(spec, "different-dataset")


def test_http_style_spec_cannot_expose_final_holdout_by_changing_trial(service):
    spec, _, _, _ = _case(service)
    spec["split"]["test_end_ns"] = 90
    with pytest.raises(PermissionError):
        service.create_experiment(spec, "new-trial-same-holdout")


def test_closed_native_test_end_cannot_consume_first_sealed_holdout_bar(service):
    spec, _, _, _ = _case(service)
    # Native replay consumes test_end itself. The registry read API is half-open,
    # so the service must guard through test_end+1 rather than test_end.
    spec["split"]["test_end_ns"] = 80
    with pytest.raises(PermissionError):
        service.create_experiment(spec, "exact-holdout-first-bar")


def _passing_evidence():
    return {rule.name: rule.threshold for rule in (*INTEGRITY, *STRATEGY, *FORWARD)}


def test_unbound_forward_evidence_cannot_sign_live_for_arbitrary_account(service):
    spec, strategy, fee, _ = _case(service)
    job = service.create_experiment(spec, "experiment")
    experiment_hash = job["payload"]["experiment_hash"]
    service.repository.put(service.tenant, "experiment_result", {
        "gate_evidence": _passing_evidence(),
    }, experiment_hash)
    service.repository.put(service.tenant, "acceptance", {
        "experiment_hash": experiment_hash,
        "observations": _passing_evidence(),
        "tested_account_id": "funded-account-A",
        "tested_provider": "frozen-provider-A",
    }, experiment_hash)
    report = service.save_validation(experiment_hash, "live_canary")
    identity = content_hash([service.tenant, "deployment", "live-account-B"])
    now = time.time_ns()
    deployment = {
        "deployment_id": identity, "strategy_hash": strategy["entity_id"],
        "provider": "changed-provider-B", "model_id": "changed-model-B",
        "prompt_hash": "d" * 64, "tools_hash": "e" * 64,
        "risk_policy_hash": service.policy.fingerprint(),
        "fee_schedule_hashes": [fee["entity_id"]],
        "validation_report_hash": report["entity_id"], "venue": "binance",
        "account_id": "funded-account-B", "mode": "live_canary",
        "risk_budget": strategy["payload"]["risk_budget"],
        "valid_from_ns": now - 1_000_000_000, "valid_until_ns": now + 60_000_000_000,
    }
    with pytest.raises((ValueError, Conflict)):
        service.create_deployment(deployment, "live-account-B")


def test_no_amount_of_trial_counts_manufactures_missing_gate_evidence():
    policy = GatePolicy()
    verdict = policy.evaluate("live_canary", {
        "submitted_candidates": 1_000_000,
        "global_selection_bias_corrected": True,
        "account_eligibility_verified": True,
    })
    assert verdict["eligible"] is False
    assert any(check["status"] == "insufficient" for check in verdict["checks"])


def test_prediction_market_cannot_skip_market_specific_probability_evidence(service):
    spec, _, _, _ = _case(service, venue="polymarket", instrument_type="prediction")
    identity = content_hash(spec)
    service.repository.put(service.tenant, "experiment_spec", spec, identity)
    service.repository.put(service.tenant, "experiment_result", {
        "gate_evidence": _passing_evidence(),
    }, identity)
    verdict = service.validation(identity, "live_canary")
    names = {check["name"] for check in verdict["checks"]}
    assert {"settled_event_clusters", "brier_skill_lcb", "ece", "rules_verified"} <= names
    assert verdict["eligible"] is False


@pytest.mark.parametrize("field", ["duplicate_effects", "account_difference", "holm_p"])
def test_negative_counts_discrepancy_and_pvalue_cannot_pass_gate(field):
    evidence = _passing_evidence()
    evidence[field] = -1
    verdict = GatePolicy().evaluate("live_canary", evidence)
    assert verdict["eligible"] is False


def test_stale_research_lease_cannot_write_or_finish(service):
    repo = service.repository
    repo.enqueue(service.tenant, "review-job", {"question": "frozen"}, "run")
    first = repo.claim("review-job", lease_seconds=1)
    assert first is not None
    second = repo.claim("review-job", now_ns=first["lease_until_ns"] + 1)
    assert second is not None and second["lease_token"] != first["lease_token"]
    with pytest.raises(Conflict, match="lease_lost"):
        repo.put(service.tenant, "research_result", {"unsafe": True}, "stale-result",
                 fence_job=(first["job_id"], first["lease_token"]))
    with pytest.raises(Conflict, match="lease_lost"):
        repo.finish(first, {"unsafe": True})
    with pytest.raises(KeyError):
        repo.get(service.tenant, "research_result", "stale-result")


def test_expired_job_cannot_be_marked_successful_while_lease_remains(service):
    repo = service.repository
    repo.enqueue(service.tenant, "review-job", {"question": "frozen"}, "run")
    claimed = repo.claim("review-job")
    with repo.transaction() as db:
        db.execute(update(jobs).where(jobs.c.job_id == claimed["job_id"]).values(deadline_ns=time.time_ns() - 1))
    with pytest.raises(Conflict):
        repo.finish(claimed, {"should_not_be_success": True})


def test_cancellation_preserves_no_future_model_or_artifact_side_effects(service):
    repo = service.repository
    queued = repo.enqueue(service.tenant, "review-job", {"question": "frozen"}, "run")
    claimed = repo.claim("review-job")
    repo.cancel(service.tenant, queued["job_id"])
    with pytest.raises(Conflict):
        repo.put(service.tenant, "research_result", {"unsafe": True}, "cancelled-result",
                 fence_job=(claimed["job_id"], claimed["lease_token"]))
    assert repo.finish(claimed, {"cancelled": True})["status"] == "CANCELLED"


def test_paid_call_reservation_is_not_replayed_and_actuals_remain_immutable(service):
    usage = UsageLedger(service.repository)
    arguments = (service.tenant, "run", "logical-call", "a" * 64, Decimal("0.20"))
    usage.reserve(*arguments, run_maximum=Decimal("1"))
    with pytest.raises(Conflict):
        usage.reserve(*arguments, run_maximum=Decimal("1"))
    usage.complete(service.tenant, "logical-call", Decimal("0.10"))
    usage.complete(service.tenant, "logical-call", Decimal("0.10"))
    with pytest.raises(Conflict):
        usage.complete(service.tenant, "logical-call", Decimal("0.01"))
    with service.repository.transaction() as db:
        assert db.execute(select(model_calls.c.actual)).scalar_one() == Decimal("0.10")
        assert set(db.execute(select(usage_budgets.c.reserved)).scalars()) == {Decimal("0.10")}


def test_corrupted_artifact_and_cross_tenant_read_are_rejected(service):
    repo = service.repository
    item = repo.put(service.tenant, "review", {"value": "frozen"}, "identity")
    with pytest.raises(KeyError):
        repo.get("other-tenant", "review", item["entity_id"])
    with repo.transaction() as db:
        db.execute(update(artifacts).where(
            artifacts.c.tenant_id == service.tenant, artifacts.c.kind == "review",
        ).values(payload={"value": "tampered"}))
    with pytest.raises(Conflict, match="integrity"):
        repo.get(service.tenant, "review", item["entity_id"])


def test_research_tools_cannot_override_identity_or_request_orders():
    from qt.research.graph import ToolBroker, ToolCall

    observed = []
    broker = ToolBroker({"evidence.read": lambda tenant, **kwargs: observed.append(tenant) or {"id": "evidence"}})
    with pytest.raises(ValueError, match="identity"):
        broker.invoke("review", ToolCall(name="evidence.read", arguments={"tenant_id": "other-tenant"}))
    with pytest.raises(ValueError, match="not_authorized"):
        broker.invoke("review", ToolCall(name="orders.submit", arguments={}))
    assert observed == []


def test_graph_refuses_provider_without_enforceable_token_limits_before_spend(service):
    pytest.importorskip("langgraph", reason="requires Python 3.12 production extra")
    from langgraph.checkpoint.memory import InMemorySaver

    from qt.research.graph import ResearchGraph, ToolBroker

    class UnboundedProvider:
        enforces_token_limits = False

        def __init__(self):
            self.calls = 0

        async def invoke(self, _request, _budget):
            self.calls += 1
            raise AssertionError("unbounded paid provider must not be invoked")

    provider = UnboundedProvider()
    service.repository.enqueue(service.tenant, "research", {"question": "frozen"}, "research")
    job = service.repository.claim("research")
    with pytest.raises((ValueError, Conflict)):
        graph = ResearchGraph(service.repository, provider, ToolBroker({}), InMemorySaver())
        asyncio.run(graph.run(job, model_id="frozen-model"))
    assert provider.calls == 0
