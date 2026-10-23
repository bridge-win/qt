"""Stage A contracts: execution recovery, evidence bounds, planner, ledgers, schedules."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from qt.agent_hub.config import ROLES, HubConfig, Role
from qt.agent_hub.context import ContextStore
from qt.agent_hub.evaluation import Discovery, evaluate
from qt.agent_hub.forecast_ledger import ForecastLedger, net_edge_payload
from qt.agent_hub.governance import proposal
from qt.agent_hub.planner import ResearchLoop
from qt.agent_hub.runtime import HubRuntime
from qt.agent_hub.store import HubStore
from qt.agent_hub.trial_ledger import TrialLedger
from tests.test_agent_hub_runtime import (
    TENANT,
    TOKEN,
    FixtureMarket,
    FixtureProvider,
    OfflineResearch,
    OfflineSandbox,
    _drain,
    _enqueue,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("runtime tests must not access the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(
        "qt.agent_hub.sources.collect",
        lambda *args, **kwargs: {"items": [], "failures": [], "retrieved_at": time.time(), "fixture": True},
    )
    monkeypatch.setattr("qt.agent_hub.opportunities.OpportunityScanner.scan", Discovery.scan)
    monkeypatch.setenv("QT_AGENT_HUB_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("QT_AGENT_HUB_EXECUTOR_TOKEN", TOKEN)


@pytest.fixture
def config() -> HubConfig:
    return HubConfig(
        tenant_id=TENANT,
        mode="paper",
        auto_execute=False,
        roles={name: Role(instruction=f"Independent instruction for {name}") for name in ROLES},
    )


@pytest.fixture
def public_quotes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    def get_json(url: str, params: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "symbol": "BTCUSDT",
            "bidPrice": "99.9",
            "askPrice": "100",
            "bidQty": "10",
            "askQty": "10",
        }

    monkeypatch.setattr("qt.agent_hub.trading._get_json", get_json)
    return []


@pytest.fixture
def runtime(tmp_path: Path, config: HubConfig, public_quotes: list[str]) -> HubRuntime:
    return HubRuntime(
        config,
        tmp_path / "state",
        tmp_path / "files",
        provider=FixtureProvider(),
        market=FixtureMarket(),
        sandbox=OfflineSandbox(),
        research=OfflineResearch(),
    )


def _paper(runtime: HubRuntime) -> None:
    runtime.config = runtime.config.model_copy(update={"auto_execute": True})
    runtime.provider = FixtureProvider(decision="propose")
    runtime.context.ingest(TENANT, "fixture:trade", "BTC evidence for a paper-only experiment")


def _crash_and_recover(runtime: HubRuntime, point: str, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [time.time()]
    monkeypatch.setattr("qt.agent_hub.store._now", lambda: now[0])
    _paper(runtime)
    runtime.crash_after = point
    root = _enqueue(runtime, "pipeline", question="BTC evidence", auto_execute=True)
    with pytest.raises(RuntimeError, match="injected_crash"):
        for _ in range(20):
            if runtime.run_once("crashing-worker") is None:
                break
    runtime.crash_after = None
    now[0] += runtime.config.lease_seconds + 5
    jobs = _drain(runtime)
    executed = next(job for job in jobs if job["kind"] == "execute")
    assert executed["result"]["status"] == "FILLED"
    assert len(runtime.trading.orders(TENANT)) == 1
    effect = runtime.trading.get_effect(TENANT, f"pipeline:{root['id']}")
    assert effect is not None and effect["status"] == "FILLED"


@pytest.mark.parametrize(
    "point",
    ["quote_ready", "intent_persisted", "order_submitted", "before_complete"],
)
def test_four_crash_points_recover_a_single_fill(
    runtime: HubRuntime, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    _crash_and_recover(runtime, point, monkeypatch)


def test_expired_and_out_of_bound_proposals_cannot_execute(runtime: HubRuntime) -> None:
    runtime.config = runtime.config.model_copy(update={"auto_execute": True})
    runtime.provider = FixtureProvider(decision="propose")
    runtime.context.ingest(TENANT, "fixture:trade", "BTC evidence for a paper-only experiment")
    root = _enqueue(runtime, "pipeline", question="BTC evidence", auto_execute=False)
    jobs = _drain(runtime)
    forecast_job = next(job for job in jobs if job["kind"] == "forecast")
    previous = {
        "forecast": forecast_job["result"],
        "risk_review": next(job for job in jobs if job["kind"] == "risk_review")["result"],
        "backtest": next(job for job in jobs if job["kind"] == "backtest")["result"],
    }
    expired = dict(previous)
    expired["forecast"] = dict(previous["forecast"])
    expired["forecast"]["answer"] = dict(previous["forecast"]["answer"], horizon_seconds=0.001)
    expired["forecast"]["proposal"] = proposal(
        expired["forecast"],
        previous["backtest"].get("experiment"),
        previous["forecast"]["config_sha256"],
    )
    expired["risk_review"] = dict(previous["risk_review"])
    expired["risk_review"]["reviewed_proposal_hash"] = expired["forecast"]["proposal"]["proposal_hash"]
    held = runtime.execute(
        TENANT, {"id": "exec-expired", "payload": {"pipeline_id": root["id"], "auto_execute": True}}, expired
    )
    assert held["status"] == "held"
    assert held["reason"] in {"evidence_expired", "forecast_horizon_expired"}
    bounded = dict(previous)
    bounded["forecast"] = dict(previous["forecast"])
    stored = dict(previous["forecast"]["proposal"])
    boundary = dict(stored.get("time_boundary") or {})
    created = float(boundary.get("created_at") or time.time())
    stored["time_boundary"] = {**boundary, "created_at": created, "knowledge_cutoff": created + 30}
    bounded["forecast"]["proposal"] = stored
    outside = runtime.execute(
        TENANT, {"id": "exec-bound", "payload": {"pipeline_id": root["id"], "auto_execute": True}}, bounded
    )
    assert outside["status"] == "held"
    assert outside["reason"] == "evidence_outside_time_boundary"


def test_planner_thirty_questions_match_single_invoke_without_tools() -> None:
    questions = [f"Assess BTC evidence case {index}" for index in range(30)]
    loop = ResearchLoop(max_rounds=4, max_tool_calls=12)

    def invoke(role: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
        return FixtureProvider().invoke(role, task, context)

    def tools(name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("fixture providers must not request tools")

    for question in questions:
        context = {"query": question, "references": [{"id": "doc-1"}]}
        once = invoke("research", question, context)
        bounded = loop.run(invoke=invoke, execute_tool=tools, role="research", task=question, context=context)
        assert bounded["planner"]["tool_calls"] == 0
        assert bounded["planner"]["rounds"][0]["decision"] != "continue" or not bounded["planner"]["rounds"][0]["tool_requests"]
        assert once["evidence"] == bounded["evidence"]


def test_trial_and_forecast_ledgers_record_search_and_scores(tmp_path) -> None:
    trials = TrialLedger(tmp_path / "trials.sqlite3")
    forecasts = ForecastLedger(tmp_path / "forecasts.sqlite3")
    row = trials.record(
        "alpha",
        {
            "snapshot_id": "snap",
            "strategy_spec_hash": "s",
            "oos_sharpe": 0.4,
            "arm": "ma_trend",
        },
    )
    summary = trials.summary("alpha", "snap", [0.4, 0.2])
    assert summary["n_trials"] == 1
    assert summary["walk_forward"]["folds"] == 2
    assert summary["cpcv"]["paths"] == 0
    assert summary["cpcv"]["status"] == "not_applicable"
    assert summary["search_accounting"]["global_selection_bias_corrected"] is False
    assert summary["holdout"]["sealed"] is True
    assert trials.unlock_holdout(
        "alpha", row["trial_id"], actor_id="validator",
        permissions=frozenset({"holdout:unlock"}), reason="frozen candidate final evaluation",
    )["once"] is True
    now = time.time()
    forecasts.record_forecast(
        "alpha",
        {
            "forecast_id": "f1",
            "market_id": "m1",
            "p_model": 0.7,
            "p_market_mid": 0.55,
            "decision_time": now - 10,
        },
    )
    forecasts.resolve("alpha", "f1", {"outcome": 1, "settle_ts": now - 1, "settle_source": "chainlink-twap"})
    scored = forecasts.score("alpha")
    assert scored["status"] == "evaluated" and scored["samples"] == 1
    assert net_edge_payload({"estimated_probability": None, "best_ask": 0.5, "fee_at_p": 0.01})["net_edge"] is None


def test_schedule_catchup_and_skip_if_running(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1_000.0]
    monkeypatch.setattr("qt.agent_hub.store._now", lambda: clock[0])
    store = HubStore(tmp_path / "hub.sqlite3")
    store.put_schedule(
        "team",
        "monitor",
        "monitor",
        {},
        60,
        overlap_policy="skip_if_running",
        catchup_window_s=90,
    )
    assert store.tick_schedules("team") == 1
    claimed = store.claim("team", "w1")
    assert claimed is not None
    clock[0] = 1_200
    assert store.tick_schedules("team") == 0
    missed = [item for item in store.events("team") if item["event_type"] == "schedule.missed"]
    assert missed and missed[-1]["payload"]["reason"] in {"skip_if_running", "catchup_window"}


def test_short_memory_is_run_scoped(tmp_path) -> None:
    store = ContextStore(tmp_path / "ctx.sqlite3", tmp_path / "files")
    store.remember("alpha", "job:other", "foreign", scope="short", source="run:other")
    store.remember("alpha", "job:mine", "local", scope="short", source="run:mine")
    store.remember("alpha", "pref", "keep", scope="long", source="owner")
    assembled = store.assemble("alpha", "BTC", run_id="mine")
    keys = {item["key"] for item in assembled["memories"]}
    assert "pref" in keys and "job:mine" in keys and "job:other" not in keys
    store.ingest(
        "alpha",
        "note",
        "BTC paragraph one.\n\nSecond paragraph about fees.",
        available_at=time.time(),
        provenance="unit-test",
    )
    assert store.time_leakage("alpha", "BTC", as_of=time.time() - 3600)["time_leakage_rate"] == 0


def test_classified_evaluation_includes_expired_evidence() -> None:
    report = evaluate()
    names = {case["id"]: case for case in report["cases"]}
    assert names["forged_evidence"]["passed"]
    assert names["expired_evidence"]["passed"]
    assert names["expired_evidence"]["actual"] == "held"
    assert report["passed"]


def test_inbox_and_cors_allow_workbench_origin(runtime: HubRuntime) -> None:
    from fastapi.testclient import TestClient

    from qt.agent_hub.api import create_app
    from tests.test_agent_hub_runtime import HEADERS

    with TestClient(create_app(runtime.config, runtime.root, runtime.files, runtime=runtime)) as client:
        inbox = client.get("/api/inbox", headers=HEADERS)
        assert inbox.status_code == 200
        assert "approvals" in inbox.json()
        allowed = client.get(
            "/api/inbox",
            headers={**HEADERS, "Origin": "http://127.0.0.1:8877"},
        )
        assert allowed.status_code == 200
        assert allowed.headers.get("access-control-allow-origin") == "http://127.0.0.1:8877"
        metrics = client.get("/metrics", headers=HEADERS)
        assert "qt_agent_hub_unknown_orders" in metrics.text
