"""Container acceptance with a real Codex call, public quotes and paper fills."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid

import httpx

from qt.agent_hub.config import files_root, load_config, state_root
from qt.agent_hub.context import ContextStore
from qt.agent_hub.sandbox import SandboxRunner
from qt.agent_hub.trading import TradingService


def main() -> None:
    token = os.environ["QT_AGENT_HUB_SERVICE_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}
    executor_headers = {"Authorization": f"Bearer {os.environ['QT_AGENT_HUB_EXECUTOR_TOKEN']}"}
    checks = {}
    with httpx.Client(timeout=180, trust_env=False) as client:
        assert client.get("http://127.0.0.1:8878/healthz").status_code == 200
        assert client.get("http://127.0.0.1:8878/api/status").status_code == 401
        status = client.get("http://127.0.0.1:8878/api/status", headers=headers).json()
        assert status["mode"] == load_config().mode
        assert (
            client.get("http://127.0.0.1:8878/api/status", headers=executor_headers).status_code
            == 401
        )
        checks["api_authentication"] = "passed"
        checks["workers"] = status["workers"]
        expected_workers = {"scheduler", "worker-1", "worker-2", "market-worker"}
        assert {
            item["worker_id"] for item in status["workers"] if item["healthy"]
        } >= expected_workers
        for url in ("http://codex:8091", "http://codex-2:8091"):
            response = client.get(url + "/healthz")
            response.raise_for_status()
            assert response.json()["ready"] is True
        checks["codex_executors_ready"] = "passed"
        model_request = {
            "role": "analysis",
            "task": "Check the supplied arithmetic. Cite fixture:arithmetic. Do not propose a trade.",
            "context": {"observations": [{"id": "fixture:arithmetic", "claim": "2+2=4"}]},
        }
        assert (
            client.post("http://codex:8091/invoke", headers=headers, json=model_request).status_code
            == 401
        )
        checks["owner_executor_token_separation"] = "passed"
        # Contention on one OAuth identity is queueing, not failed authentication.
        for _ in range(36):
            response = client.post(
                "http://codex:8091/invoke", headers=executor_headers, json=model_request
            )
            if response.status_code != 429:
                break
            time.sleep(5)
        response.raise_for_status()
        answer = response.json()
        assert answer["side"] == "HOLD" and answer["decision"] in {"hold", "reject", "propose"}
        checks["actual_codex_oauth_invocation"] = {"passed": True, "decision": answer["decision"]}
    context = ContextStore(state_root() / "context.sqlite3", files_root())
    context.ingest(
        "verification", "fixture:research", "BTC research requires evidence and transaction costs."
    )
    assert context.search("verification", "transaction costs")
    assert not context.search("unrelated-tenant", "transaction costs")
    key = "verify-" + uuid.uuid4().hex
    context.remember("verification", key, "confirmed preference", scope="long")
    assert context.forget("verification", key)
    checks["retrieval_memory_tenant_isolation"] = "passed"
    result = SandboxRunner(state_root() / "experiments").run(
        "verification",
        uuid.uuid4().hex,
        {
            "task": "backtest",
            "prices": [100 + i * 0.5 + (i % 7) for i in range(120)],
            "window": 20,
            "initial_cash": 1000,
            "fee_bps": 10,
        },
    )
    assert result["status"] == "completed" and result["metrics"]["fills_count"] > 0
    checks["isolated_numerical_backtest"] = {"passed": True, "input_hash": result["input_hash"]}
    # Separate paper account: acceptance cannot select a production execution mode.
    trading = TradingService(state_root() / "verification-trading.sqlite3")
    tenant = "verify-" + uuid.uuid4().hex[:12]
    quote = trading.quote("binance", "BTCUSDT")
    intent = {
        "idempotency_key": "buy",
        "venue": "binance",
        "instrument": "BTCUSDT",
        "side": "BUY",
        "quantity": 10 / quote["ask"],
        "limit_price": quote["ask"] * 1.002,
        "quote_timestamp": quote["timestamp"],
    }
    bought = trading.submit(tenant, intent)
    assert bought["status"] == "FILLED"
    assert trading.submit(tenant, intent)["order_id"] == bought["order_id"]
    quote = trading.quote("binance", "BTCUSDT")
    sold = trading.submit(
        tenant,
        {
            "idempotency_key": "sell",
            "venue": "binance",
            "instrument": "BTCUSDT",
            "side": "SELL",
            "quantity": bought["filled_qty"],
            "limit_price": quote["bid"] * 0.998,
            "quote_timestamp": quote["timestamp"],
        },
    )
    assert sold["status"] == "FILLED" and len(trading.orders(tenant)) == 2
    checks["real_btc_quote_paper_roundtrip_idempotency"] = "passed"
    try:
        markets = trading.discover_markets("bitcoin", limit=3)
        checks["polymarket_public_discovery"] = {"passed": bool(markets), "markets": len(markets)}
        attempts = 0
        book_check = {"passed": False, "reason": "no_ready_book_in_bounded_sample"}
        for market in markets:
            for outcome in market["outcomes"][:2]:
                attempts += 1
                try:
                    prediction = trading.quote("polymarket", outcome["token_id"])
                except (ValueError, httpx.HTTPError):
                    continue
                if prediction["ready"]:
                    book_check = {"passed": True, "source": prediction["source"]}
                    break
            if book_check["passed"]:
                break
        checks["polymarket_orderbook"] = {**book_check, "sampled_tokens": attempts}
    except (ValueError, httpx.HTTPError):
        checks["polymarket_public_discovery"] = {"passed": False, "reason": "upstream_unavailable"}
    report = {"timestamp": time.time(), "checks": checks, "real_money_orders_sent": False}
    destination = state_root() / "verification-report.json"
    destination.write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "report": str(destination),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                **report,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
