"""Offline end-to-end workflow contracts with real SQLite domain ledgers."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from qt.agent_hub.api import create_app
from qt.agent_hub.codex import CodexBusyError, _home_lock
from qt.agent_hub.codex_service import create_codex_app
from qt.agent_hub.config import ROLES, HubConfig, Role
from qt.agent_hub.context import ContextStore
from qt.agent_hub.runtime import HubRuntime, RemoteCodex
from qt.agent_hub.sandbox import validate_request
from qt.agent_hub.sandbox_service import create_sandbox_app, experiment
from qt.agent_hub.store import HubStore
from qt.agent_hub.trading import TradingError, TradingService

TOKEN = "offline-runtime-fixture-token"
TENANT = "alpha"
HEADERS = {"Authorization": "Bearer " + TOKEN}


class FixtureProvider:
    def __init__(self, *, failure: bool = False, decision: str = "hold") -> None:
        self.calls: list[dict[str, Any]] = []
        self.failure, self.decision = failure, decision

    def invoke(self, role: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"role": role, "task": task, "context": copy.deepcopy(context)})
        if self.failure:
            raise RuntimeError("private-provider-response-must-not-enter-ledger")
        evidence = [item["id"] for item in context.get("references", [])]
        return {
            "summary": role + " independently reviewed supplied evidence",
            "confidence": 0.95,
            "decision": self.decision if role in {"forecast", "risk_review"} else "hold",
            "evidence": evidence or ["supplied-market-snapshot"],
            "actions": [],
            "probability": 0.65 if role == "forecast" else None,
            "side": "BUY" if role == "forecast" else "HOLD",
            "horizon_seconds": 3600 if role == "forecast" else None,
        }


class FixtureMarket:
    def history(self) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": 1_700_000_000_000 + index * 86_400_000,
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "volume": 1_000,
            }
            for index in range(60)
        ]


class OfflineSandbox:
    """Dependency-injected numerical transport, running the actual fixed function."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def run(self, tenant_id: str, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(
            {"tenant_id": tenant_id, "job_id": job_id, "request": copy.deepcopy(request)}
        )
        if self.response is not None:
            return dict(self.response)
        return {**experiment(validate_request(request)), "status": "completed", "job_id": job_id}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("runtime tests must not access the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(
        "qt.agent_hub.sources.collect",
        lambda *args, **kwargs: {
            "items": [],
            "failures": [],
            "retrieved_at": time.time(),
            "fixture": True,
        },
    )
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
    calls: list[str] = []

    def get_json(url: str, params: dict[str, str] | None = None) -> dict[str, str]:
        assert url in {
            "https://data-api.binance.vision/api/v3/ticker/bookTicker",
            "https://testnet.binance.vision/api/v3/ticker/bookTicker",
        }
        assert params == {"symbol": "BTCUSDT"}
        calls.append(url)
        return {
            "symbol": "BTCUSDT",
            "bidPrice": "99.9",
            "askPrice": "100",
            "bidQty": "10",
            "askQty": "10",
        }

    monkeypatch.setattr("qt.agent_hub.trading._get_json", get_json)
    return calls


@pytest.fixture
def runtime(tmp_path: Path, config: HubConfig, public_quotes: list[str]) -> HubRuntime:
    root, files = tmp_path / "state", tmp_path / "files"
    return HubRuntime(
        config,
        root,
        files,
        provider=FixtureProvider(),
        market=FixtureMarket(),
        sandbox=OfflineSandbox(),
    )


@pytest.fixture
def client(runtime: HubRuntime) -> Iterator[TestClient]:
    with TestClient(
        create_app(runtime.config, runtime.root, runtime.files, runtime=runtime)
    ) as browser:
        yield browser


def _enqueue(runtime: HubRuntime, kind: str, **payload: object) -> dict[str, Any]:
    return runtime.store.enqueue(TENANT, kind, dict(payload), idempotency_key=f"test:{kind}")


def _drain(runtime: HubRuntime) -> list[dict[str, Any]]:
    jobs = []
    for _ in range(20):
        job = runtime.run_once("integration-worker")
        if job is None:
            break
        jobs.append(job)
    else:
        raise AssertionError("workflow did not become idle")
    return jobs


def test_api_authentication_origin_and_safe_response_headers(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    for path in ("/api/status", "/api/jobs", "/api/events", "/api/portfolio", "/metrics"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/api/status", headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert (
        client.get(
            "/api/status", headers={**HEADERS, "Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/jobs", headers={**HEADERS, "Content-Length": "1000001"}, json={}
        ).status_code
        == 413
    )


def test_api_never_accepts_client_owned_tenant(client: TestClient, runtime: HubRuntime) -> None:
    foreign = runtime.store.enqueue(
        "bravo", "research", {"private": "foreign"}, idempotency_key="foreign"
    )
    for endpoint, body in (
        ("/api/jobs", {"kind": "research", "tenant_id": "bravo"}),
        ("/api/documents", {"source": "report", "content": "private", "tenant_id": "bravo"}),
        ("/api/memories", {"key": "preference", "value": "private", "tenant_id": "bravo"}),
        ("/api/orders", {"tenant_id": "bravo"}),
    ):
        assert client.post(endpoint, headers=HEADERS, json=body).status_code == 422
    assert client.get(f"/api/jobs/{foreign['id']}", headers=HEADERS).status_code == 404
    assert client.post(f"/api/jobs/{foreign['id']}/retry", headers=HEADERS).status_code == 404
    assert client.get("/api/jobs?tenant_id=bravo", headers=HEADERS).json() == {"items": []}
    assert runtime.store.list_jobs(TENANT) == []


@pytest.mark.parametrize(
    "source", ["../../outside", "/etc/passwd", "..\\outside", "mount:../../outside"]
)
def test_api_rejects_document_path_attacks(client: TestClient, source: str) -> None:
    response = client.post(
        "/api/documents", headers=HEADERS, json={"source": source, "content": "not stored"}
    )
    assert response.status_code == 422


def test_api_artifact_file_symlink_and_directory_traversal_are_rejected(
    client: TestClient,
    runtime: HubRuntime,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "private.json"
    outside.write_text('{"secret":"outside"}')
    directory = runtime.files / TENANT / "artifacts"
    directory.mkdir(parents=True)
    (directory / "deadbeef.json").symlink_to(outside)
    assert client.get("/api/artifacts/deadbeef.json", headers=HEADERS).status_code == 404
    for name in ("%2e%2e%2fprivate.json", "..%5cprivate.json", "private.txt", "a" * 300 + ".json"):
        assert client.get("/api/artifacts/" + name, headers=HEADERS).status_code == 404


def test_api_artifact_parent_directory_symlink_is_rejected(
    client: TestClient,
    runtime: HubRuntime,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "deadbeef.json").write_text('{"secret":"must not be served"}')
    tenant = runtime.files / TENANT
    tenant.mkdir(parents=True, exist_ok=True)
    (tenant / "artifacts").symlink_to(outside, target_is_directory=True)
    response = client.get("/api/artifacts/deadbeef.json", headers=HEADERS)
    assert response.status_code == 404
    assert "must not be served" not in response.text


def test_import_job_cannot_follow_a_symlinked_import_directory(
    runtime: HubRuntime, tmp_path: Path
) -> None:
    outside = tmp_path / "external-imports"
    outside.mkdir()
    (outside / "private.txt").write_text("outside-private-corpus")
    tenant = runtime.files / TENANT
    tenant.mkdir(parents=True, exist_ok=True)
    (tenant / "imports").symlink_to(outside, target_is_directory=True)
    _enqueue(runtime, "import_context")
    runtime.run_once("import-worker")
    assert runtime.context.search(TENANT, "outside-private-corpus") == []


def test_pipeline_runs_six_independent_roles_in_order_and_persists_links(
    runtime: HubRuntime,
) -> None:
    assert isinstance(runtime.store, HubStore)
    assert isinstance(runtime.context, ContextStore)
    assert isinstance(runtime.trading, TradingService)
    evidence = runtime.context.ingest(
        TENANT, "fixture:research", "BTC evidence market experiment baseline"
    )
    root_job = _enqueue(runtime, "pipeline", question="BTC evidence market", auto_execute=False)
    jobs = _drain(runtime)
    assert [job["kind"] for job in jobs] == ["pipeline", *ROLES]
    assert all(job["status"] == "succeeded" for job in jobs)
    provider = runtime.provider
    assert isinstance(provider, FixtureProvider)
    assert [call["role"] for call in provider.calls] == list(ROLES)
    for index, call in enumerate(provider.calls):
        assert call["task"].startswith(f"Independent instruction for {ROLES[index]}")
        assert evidence["id"] in [item["id"] for item in call["context"]["references"]]
        if index:
            assert ROLES[index - 1] in call["context"]["previous_steps"]
    for previous, current in pairwise(jobs):
        assert current["payload"]["parent_id"] == previous["id"]
        assert current["payload"]["pipeline_id"] == root_job["id"]
    assert len(runtime.sandbox.calls) == 1
    backtest = next(job for job in jobs if job["kind"] == "backtest")
    assert backtest["result"]["experiment"]["status"] == "completed"
    assert runtime.store.summary(TENANT)["counts"]["queued"] == 0
    assert runtime.trading.orders(TENANT) == []
    assert len(runtime.context.memories(TENANT, "short")) == 6


def test_pipeline_continues_after_process_recreation_with_same_ledger(runtime: HubRuntime) -> None:
    _enqueue(runtime, "pipeline", question="recovery evidence", auto_execute=False)
    first = runtime.run_once("old-worker")
    assert first is not None and first["status"] == "succeeded"
    recreated = HubRuntime(
        runtime.config,
        runtime.root,
        runtime.files,
        provider=FixtureProvider(),
        market=FixtureMarket(),
        sandbox=OfflineSandbox(),
    )
    jobs = _drain(recreated)
    assert [job["kind"] for job in jobs] == list(ROLES)
    assert len(recreated.store.list_jobs(TENANT)) == 7
    assert recreated.run_once("third-worker") is None


def test_provider_failure_retries_then_deadletters_without_fake_artifacts(
    runtime: HubRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [time.time()]
    monkeypatch.setattr("qt.agent_hub.store._now", lambda: now[0])
    runtime.provider = FixtureProvider(failure=True)
    original = runtime.store.enqueue(
        TENANT,
        "research",
        {"question": "evidence"},
        idempotency_key="failing-provider",
        max_attempts=2,
    )
    first = runtime.run_once("worker")
    assert first is not None and first["status"] == "queued"
    assert first["attempts"] == 1
    assert runtime.run_once("worker") is None
    now[0] += 31
    second = runtime.run_once("worker")
    assert second is not None and second["status"] == "deadletter"
    assert second["attempts"] == 2
    assert runtime.run_once("worker") is None
    assert len(runtime.provider.calls) == 2
    assert not (runtime.files / TENANT / "artifacts" / f"{original['id']}.json").exists()
    assert runtime.context.memories(TENANT) == []
    assert "private-provider-response" not in json.dumps(runtime.store.events(TENANT))


def test_late_expired_worker_cannot_overwrite_a_recovered_result_artifact_or_memory(
    runtime: HubRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [time.time()]
    monkeypatch.setattr("qt.agent_hub.store._now", lambda: now[0])
    queued = _enqueue(runtime, "research", question="evidence")
    recovered = HubRuntime(
        runtime.config,
        runtime.root,
        runtime.files,
        provider=FixtureProvider(),
        market=FixtureMarket(),
        sandbox=OfflineSandbox(),
    )

    class LateProvider(FixtureProvider):
        def invoke(self, role: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
            now[0] += runtime.config.lease_seconds + 1
            takeover = recovered.run_once("new-worker")
            assert takeover is not None and takeover["status"] == "succeeded"
            answer = super().invoke(role, task, context)
            answer["summary"] = "stale output must not replace accepted output"
            return answer

    runtime.provider = LateProvider()
    runtime.run_once("expired-worker")
    saved = runtime.store.get_job(TENANT, str(queued["id"]))
    assert saved is not None and saved["status"] == "succeeded"
    assert (
        saved["result"]["answer"]["summary"] == "research independently reviewed supplied evidence"
    )
    artifact = saved["result"]["artifact"]
    artifact_path = runtime.files / TENANT / artifact["path"]
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == artifact["sha256"]
    memory = next(
        item for item in runtime.context.memories(TENANT) if item["key"] == f"job:{queued['id']}"
    )
    answer = json.loads(memory["value"]) if isinstance(memory["value"], str) else memory["value"]
    assert answer["summary"] == saved["result"]["answer"]["summary"]


def test_expired_execution_owner_cannot_trade_after_new_owner_commits_hold(
    runtime: HubRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.config = runtime.config.model_copy(update={"auto_execute": True})
    now = [time.time()]
    monkeypatch.setattr("qt.agent_hub.store._now", lambda: now[0])
    _enqueue(runtime, "execute", auto_execute=True)
    old = runtime.store.claim(TENANT, "old-owner", lease_seconds=60)
    assert old is not None
    now[0] += 61
    takeover = runtime.store.claim(TENANT, "new-owner", lease_seconds=60)
    assert takeover is not None and takeover["lease_token"] > old["lease_token"]
    held = {"status": "held", "reason": "new_owner_declined_execution"}
    assert runtime.store.complete(
        TENANT, str(takeover["id"]), "new-owner", int(takeover["lease_token"]), held
    )
    quote = runtime.trading.quote("binance", "BTCUSDT")
    answer = {
        "decision": "propose",
        "confidence": 0.95,
        "evidence": ["supplied-market-snapshot"],
        "side": "BUY",
        "horizon_seconds": 3600,
    }
    previous = {
        "forecast": {"venue": "binance", "instrument": "BTCUSDT", "quote": quote, "answer": answer},
        "risk_review": {"answer": answer},
    }

    def guard() -> bool:
        if not runtime.store.heartbeat(
            TENANT, str(old["id"]), "old-owner", int(old["lease_token"]), lease_seconds=60
        ):
            raise RuntimeError("job_lease_lost")
        return True

    try:
        result = runtime.execute(TENANT, old, previous, lease_guard=guard)
    except (RuntimeError, ValueError) as error:
        assert "lease" in str(error).lower()
    else:
        assert result["status"] in {"held", "blocked"}
    assert runtime.trading.orders(TENANT) == []
    assert runtime.trading.portfolio(TENANT)["positions"] == []
    saved = runtime.store.get_job(TENANT, str(old["id"]))
    assert saved is not None and saved["result"] == held


def test_paper_pipeline_records_one_fee_accounted_fill_and_idempotent_replay(
    runtime: HubRuntime,
) -> None:
    runtime.config = runtime.config.model_copy(update={"auto_execute": True})
    runtime.provider = FixtureProvider(decision="propose")
    runtime.context.ingest(TENANT, "fixture:trade", "BTC evidence for a paper-only experiment")
    root = _enqueue(runtime, "pipeline", question="BTC evidence", auto_execute=True)
    jobs = _drain(runtime)
    assert all(job["status"] == "succeeded" for job in jobs)
    assert [call["role"] for call in runtime.provider.calls] == list(ROLES)
    executed = next(job for job in jobs if job["kind"] == "execute")
    assert executed["result"]["status"] == "FILLED"
    orders = runtime.trading.orders(TENANT)
    assert len(orders) == 1
    assert orders[0]["mode"] == "paper"
    assert float(orders[0]["fee_quote"]) > 0
    assert (
        float(orders[0]["quote_qty"]) + float(orders[0]["fee_quote"])
        <= runtime.config.max_order_quote
    )
    assert runtime.trading.portfolio(TENANT)["positions"]
    replay = _enqueue(runtime, "pipeline", question="BTC evidence", auto_execute=True)
    assert replay["id"] == root["id"]
    assert _drain(runtime) == []
    assert len(runtime.trading.orders(TENANT)) == 1


@pytest.mark.parametrize("lookup_initially_available", [True, False])
def test_monitor_recovers_unknown_execution_once_without_resending_or_bypassing_freeze(
    runtime: HubRuntime,
    lookup_initially_available: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AmbiguousExecution:
        venue = "binance"
        environment = "testnet"
        account_id = "dedicated-runtime-recovery-fixture"

        def __init__(self) -> None:
            self.sends = self.lookups = 0
            self.available = lookup_initially_available

        def balances(self, instrument: str) -> dict[str, str]:
            assert instrument == "BTCUSDT"
            return {"cash": "100", "quantity": "0"}

        def submit(self, client_id: str, intent: dict[str, Any]) -> dict[str, Any]:
            self.sends += 1
            raise TimeoutError("private-upstream-response-must-not-enter-monitor")

        def lookup(self, client_id: str, record: dict[str, Any]) -> dict[str, Any]:
            self.lookups += 1
            assert client_id == record["order_id"]
            if not self.available:
                raise TimeoutError("private-upstream-response-must-not-enter-monitor")
            return {
                "status": "FILLED",
                "side": "BUY",
                "filled_qty": "0.1",
                "quote_qty": "10",
                "fee_quote": "0.01",
                "exchange_id": "fixture-123",
            }

    runtime.config = runtime.config.model_copy(update={"mode": "testnet"})
    runtime.trading = TradingService(runtime.root / "testnet-trading.sqlite3", mode="testnet")
    adapter = AmbiguousExecution()
    runtime.trading.bind_execution(TENANT, "binance", adapter, acknowledge=True)
    intent = {
        "idempotency_key": "ambiguous-buy",
        "venue": "binance",
        "instrument": "BTCUSDT",
        "side": "BUY",
        "quantity": "0.1",
        "limit_price": "100",
        "quote_timestamp": time.time(),
    }
    submitted = runtime.trading.submit(TENANT, intent)
    assert submitted["status"] == "UNKNOWN" and adapter.sends == 1
    assert runtime.trading.portfolio(TENANT)["positions"] == []
    original_quote = runtime._quote

    def quote_after_reconciliation(payload: dict[str, Any]) -> dict[str, Any]:
        assert adapter.lookups >= 1
        assert runtime.trading.status(TENANT)["unresolved_orders"] == (
            0 if adapter.available else 1
        )
        return original_quote(payload)

    monkeypatch.setattr(runtime, "_quote", quote_after_reconciliation)
    _enqueue(runtime, "monitor")
    monitored = runtime.run_once("monitor-worker")
    assert monitored is not None and monitored["status"] == "succeeded"
    reconciliation = monitored["result"]["reconciliation"]
    assert reconciliation["resubmitted"] == 0
    if not lookup_initially_available:
        assert reconciliation["unresolved_orders"] == 1
        assert reconciliation["error"] == "unresolved_orders_reconcile_required"
        assert reconciliation["orders"] == [
            {"order_id": submitted["order_id"], "status": "UNKNOWN"}
        ]
        with pytest.raises(TradingError, match="unresolved_order_reconcile_required"):
            runtime.trading.submit(TENANT, {**intent, "idempotency_key": "blocked-new-buy"})
        assert adapter.sends == 1
        adapter.available = True
        runtime.store.enqueue(TENANT, "monitor", {}, idempotency_key="monitor-recover")
        monitored = runtime.run_once("monitor-worker")
        assert monitored is not None and monitored["status"] == "succeeded"
        reconciliation = monitored["result"]["reconciliation"]
    assert reconciliation["orders"] == [{"order_id": submitted["order_id"], "status": "FILLED"}]
    assert reconciliation["unresolved_orders"] == reconciliation["resubmitted"] == 0
    portfolio = runtime.trading.portfolio(TENANT)
    assert Decimal(portfolio["cash"]["binance"]["amount"]) == Decimal("89.99")
    assert Decimal(portfolio["positions"][0]["quantity"]) == Decimal("0.1")
    assert Decimal(portfolio["positions"][0]["cost"]) == Decimal("10.01")
    assert runtime.trading.orders(TENANT)[0]["accounted"] is True
    lookup_count = adapter.lookups
    runtime.store.enqueue(TENANT, "monitor", {}, idempotency_key="monitor-repeat")
    repeated = runtime.run_once("monitor-worker")
    assert repeated is not None and repeated["status"] == "succeeded"
    assert repeated["result"]["reconciliation"] == {
        "mode": "testnet",
        "orders": [],
        "resubmitted": 0,
        "unresolved_orders": 0,
    }
    assert runtime.trading.portfolio(TENANT) == portfolio
    assert len(runtime.trading.orders(TENANT)) == 1
    assert adapter.sends == 1 and adapter.lookups == lookup_count
    assert runtime.trading.orders("unrelated-tenant") == []
    assert "private-upstream-response" not in json.dumps(monitored)


def test_monitor_reconciliation_failure_prevents_quote_and_remains_an_explicit_job_failure(
    runtime: HubRuntime,
    public_quotes: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(tenant_id: str) -> dict[str, Any]:
        raise RuntimeError("private-reconciliation-response-must-not-enter-ledger")

    monkeypatch.setattr(runtime.trading, "reconcile", unavailable)
    _enqueue(runtime, "monitor")
    job = runtime.run_once("monitor-worker")
    assert job is not None and job["status"] == "queued"
    assert job["result"] is None and job["error"] == "RuntimeError"
    assert public_quotes == []
    assert "private-reconciliation-response" not in json.dumps(job)


def test_independent_risk_veto_prevents_paper_fill(runtime: HubRuntime) -> None:
    class RiskVeto(FixtureProvider):
        def invoke(self, role: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
            answer = super().invoke(role, task, context)
            if role == "risk_review":
                answer["decision"] = "reject"
            return answer

    runtime.config = runtime.config.model_copy(update={"auto_execute": True})
    runtime.provider = RiskVeto(decision="propose")
    _enqueue(runtime, "pipeline", question="BTC evidence", auto_execute=True)
    jobs = _drain(runtime)
    result = next(job for job in jobs if job["kind"] == "execute")["result"]
    assert result["status"] == "held"
    assert runtime.trading.orders(TENANT) == []


@pytest.mark.parametrize("status", ["failed", "unavailable"])
def test_unfinished_sandbox_cannot_become_successful_backtest(
    runtime: HubRuntime, status: str
) -> None:
    runtime.sandbox = OfflineSandbox({"status": status, "reason": "isolated_executor_not_ready"})
    queued = _enqueue(runtime, "backtest", question="validate experiment")
    job = runtime.run_once("worker")
    assert job is not None and job["status"] == "queued"
    assert job["result"] is None
    assert runtime.provider.calls == []
    assert not (runtime.files / TENANT / "artifacts" / f"{queued['id']}.json").exists()


def test_live_without_execution_binding_rejects_before_quote_and_never_creates_order(
    runtime: HubRuntime,
    public_quotes: list[str],
) -> None:
    live_config = runtime.config.model_copy(update={"mode": "live"})
    live = HubRuntime(
        live_config,
        runtime.root / "live",
        runtime.files / "live",
        provider=FixtureProvider(),
        market=FixtureMarket(),
        sandbox=OfflineSandbox(),
    )
    with TestClient(create_app(live_config, live.root, live.files, runtime=live)) as client:
        response = client.post(
            "/api/orders",
            headers=HEADERS,
            json={
                "idempotency_key": "live-not-enabled",
                "venue": "binance",
                "instrument": "BTCUSDT",
                "side": "BUY",
                "quantity": "0.1",
                "limit_price": "100",
                "quote_timestamp": time.time(),
            },
        )
        assert response.status_code == 422
        assert client.get("/api/orders", headers=HEADERS).json() == {"items": []}
    assert public_quotes == []
    assert live.trading.portfolio(TENANT)["positions"] == []


def test_mcp_auth_readonly_tool_allowlist_and_research_cannot_enable_execution(
    client: TestClient,
    runtime: HubRuntime,
) -> None:
    # Even when the owner enables execution globally, MCP submissions stay research-only.
    runtime.config = runtime.config.model_copy(update={"auto_execute": True})

    def invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = client.post(
            "/mcp",
            headers=HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert response.status_code == 200
        return response.json()

    assert client.post("/mcp", json={"method": "tools/list"}).status_code == 401
    tools = client.post(
        "/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    ).json()
    assert {tool["name"] for tool in tools["result"]["tools"]} == {
        "search_evidence",
        "read_status",
        "submit_research",
    }
    for tool in ("submit_order", "execute", "set_kill", "bind_execution"):
        assert invoke(tool, {})["error"]["code"] == -32602
    result = invoke(
        "submit_research", {"question": "evidence", "auto_execute": True, "tenant_id": "bravo"}
    )
    assert "result" in result
    queued = runtime.store.list_jobs(TENANT)
    assert len(queued) == 1
    assert queued[0]["kind"] == "pipeline"
    assert queued[0]["payload"]["auto_execute"] is False
    assert runtime.store.list_jobs("bravo") == []
    _drain(runtime)
    assert runtime.trading.orders(TENANT) == []


def test_pipeline_bounds_accumulated_prior_answers_before_provider_input(
    runtime: HubRuntime,
) -> None:
    class LargeValidAnswers(FixtureProvider):
        def invoke(self, role: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
            # Leave room for the Codex instruction envelope within its 64 KiB input limit.
            if len(json.dumps(context, ensure_ascii=False).encode()) > 60_000:
                raise ValueError("context_exceeds_provider_input_budget")
            answer = super().invoke(role, task, context)
            answer.update(
                {
                    "summary": role + ":" + "s" * 5_900,
                    "evidence": [f"source-{index}:" + "e" * 400 for index in range(30)],
                    "actions": [f"proposal-{index}:" + "a" * 400 for index in range(30)],
                }
            )
            assert len(json.dumps(answer).encode()) < 32_768
            return answer

    runtime.provider = LargeValidAnswers()
    _enqueue(runtime, "pipeline", question="bounded context evidence", auto_execute=False)
    jobs = _drain(runtime)
    assert [job["kind"] for job in jobs] == ["pipeline", *ROLES]
    assert all(job["status"] == "succeeded" for job in jobs)


def test_fixed_backtest_executes_previous_decision_next_bar_and_accounts_fees() -> None:
    request = {
        "task": "backtest",
        "prices": [100, 100, 110, 120, 90, 80],
        "window": 2,
        "initial_cash": 1_000,
        "fee_bps": 100,
        "slippage_bps": 0,
    }
    result = experiment(request)
    assert [(fill["index"], fill["side"], fill["price"]) for fill in result["fills"]] == [
        (3, "BUY", 120),
        (5, "SELL", 80),
    ]
    expected = 1_000 / 1.01 / 120 * 80 * 0.99
    assert result["metrics"]["final_equity"] == pytest.approx(expected)
    assert result["metrics"]["fees"] == pytest.approx(sum(fill["fee"] for fill in result["fills"]))
    no_fee = experiment({**request, "fee_bps": 0})
    slipped = experiment({**request, "slippage_bps": 100})
    assert (
        slipped["metrics"]["final_equity"]
        < result["metrics"]["final_equity"]
        < no_fee["metrics"]["final_equity"]
    )
    # Future changes cannot alter earlier equity or the earlier entry decision.
    changed = experiment({**request, "prices": [100, 100, 110, 120, 1_000, 10_000]})
    assert changed["equity_curve"][:4] == result["equity_curve"][:4]
    assert changed["fills"][0] == result["fills"][0]


def test_sandbox_api_requires_auth_and_rejects_arbitrary_execution() -> None:
    with TestClient(create_sandbox_app()) as client:
        body = {"task": "backtest", "prices": [100, 101, 102, 103, 104], "window": 2}
        assert client.post("/run", json=body).status_code == 401
        assert (
            client.post(
                "/run", headers=HEADERS, json={**body, "code": "print('not allowed')"}
            ).status_code
            == 422
        )
        assert (
            client.post("/run", headers=HEADERS, json={**body, "task": "shell"}).status_code == 422
        )
        response = client.post("/run", headers=HEADERS, json=body)
        assert response.status_code == 200
        assert response.json()["execution"] == "offline-fixed-function"
        assert response.json()["metrics"]["fills_count"] > 0


def test_shared_oauth_lock_busy_returns_retry_after_not_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "shared-oauth"
    monkeypatch.setenv("QT_AGENT_HUB_CODEX_HOME", str(home))
    body = {"role": "research", "task": "review evidence", "context": {}}
    with TestClient(create_codex_app()) as client:
        with _home_lock(home):
            with pytest.raises(CodexBusyError), _home_lock(home):
                raise AssertionError("the second invocation must not acquire the lock")
            response = client.post("/invoke", headers=HEADERS, json=body)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "5"
        # Missing OAuth after admission is a real error, not an infinite busy deferral.
        failed = client.post("/invoke", headers=HEADERS, json=body)
        assert failed.status_code == 503
        assert "Retry-After" not in failed.headers


def test_executor_local_semaphore_busy_returns_429_without_another_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = Event(), Event()
    calls = []

    class BlockingProvider:
        def __init__(self, **kwargs: object) -> None:
            self.last_usage: dict[str, int] = {}
            self.last_duration = 0.1

        def invoke(self, role: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
            calls.append(role)
            started.set()
            assert release.wait(5)
            return {"summary": "completed"}

    monkeypatch.setattr("qt.agent_hub.codex_service.CodexProvider", BlockingProvider)
    with TestClient(create_codex_app()) as client, ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            client.post,
            "/invoke",
            headers=HEADERS,
            json={"role": "research", "task": "first", "context": {}},
        )
        try:
            assert started.wait(3)
            second = client.post(
                "/invoke",
                headers=HEADERS,
                json={"role": "analysis", "task": "second", "context": {}},
            )
            assert second.status_code == 429
            assert second.headers["Retry-After"] == "5"
        finally:
            release.set()
        assert first.result(timeout=3).status_code == 200
    assert calls == ["research"]


@pytest.mark.parametrize("header,expected", [("7", 7), ("NaN", 5), ("9999999", 60), ("invalid", 5)])
def test_remote_executor_busy_is_typed_and_retry_delay_is_bounded(
    config: HubConfig,
    monkeypatch: pytest.MonkeyPatch,
    header: str,
    expected: float,
) -> None:
    original = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        return httpx.Response(
            429, headers={"Retry-After": header}, json={"detail": "executor busy"}
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs)
    )
    with pytest.raises(CodexBusyError) as caught:
        RemoteCodex(config).invoke("research", "review evidence", {})
    assert caught.value.retry_after == expected


def test_repeated_executor_busy_does_not_deadletter_the_six_role_pipeline(
    runtime: HubRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [time.time()]
    monkeypatch.setattr("qt.agent_hub.store._now", lambda: now[0])

    class ContendedProvider(FixtureProvider):
        def __init__(self) -> None:
            super().__init__()
            self.admissions: dict[str, int] = {}

        def invoke(self, role: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
            self.admissions[role] = self.admissions.get(role, 0) + 1
            if self.admissions[role] <= 4:
                raise CodexBusyError(retry_after=5)
            return super().invoke(role, task, context)

    runtime.provider = ContendedProvider()
    _enqueue(runtime, "pipeline", question="contention recovery evidence", auto_execute=False)
    for _ in range(50):
        pending = [job for job in runtime.store.list_jobs(TENANT) if job["status"] == "queued"]
        if not pending:
            break
        now[0] = max(now[0], min(float(job["not_before"]) for job in pending) + 0.01)
        runtime.run_once("contended-worker")
    else:
        raise AssertionError("pipeline failed to recover after admission became available")
    jobs = runtime.store.list_jobs(TENANT)
    assert len(jobs) == 7
    assert all(job["status"] == "succeeded" for job in jobs)
    assert all(job["attempts"] == 1 for job in jobs)
    assert [call["role"] for call in runtime.provider.calls] == list(ROLES)
    assert runtime.provider.admissions == dict.fromkeys(ROLES, 5)
    assert (
        sum(event["event_type"] == "job.deferred" for event in runtime.store.events(TENANT)) == 24
    )
