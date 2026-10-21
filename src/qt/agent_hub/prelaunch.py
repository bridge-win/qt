"""Live local integration acceptance through authenticated API and actual workers.

This submits only a paper-mode research workflow. It does not certify strategy
quality, live account eligibility or funded trading. Responses contain no token.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import httpx

from qt.agent_hub.config import ROLES, state_root


def main() -> None:
    headers = {"Authorization": "Bearer " + os.environ["QT_AGENT_HUB_SERVICE_TOKEN"]}
    with httpx.Client(
        base_url="http://127.0.0.1:8878", headers=headers, timeout=30, trust_env=False
    ) as client:
        status = client.get("/api/status").raise_for_status().json()
        if status["mode"] != "paper":
            raise RuntimeError("prelaunch_requires_paper_mode")
        started = time.time()
        job = (
            client.post(
                "/api/jobs",
                json={
                    "kind": "pipeline",
                    "strategy": "ma_trend",
                    "question": "Prelaunch verification: assess the exact supplied SMA20/60 strategy and BTC data. "
                    "Cite only IDs in references, inspect native OOS validation and costs. "
                    "HOLD when quality gates fail; distinguish engineering completion from forecast quality.",
                    "venue": "binance",
                    "instrument": "BTCUSDT",
                    "auto_execute": True,
                    "idempotency_key": "prelaunch:" + uuid.uuid4().hex,
                },
            )
            .raise_for_status()
            .json()
        )
        run_id = job["id"]
        print(json.dumps({"event": "prelaunch_started", "run_id": run_id}), flush=True)
        steps: list[dict[str, Any]] = []
        while time.time() - started < 1200:
            jobs = client.get("/api/jobs").raise_for_status().json()["items"]
            steps = [
                item
                for item in jobs
                if item["id"] == run_id or item["payload"].get("pipeline_id") == run_id
            ]
            if any(item["status"] == "deadletter" for item in steps):
                break
            if any(
                item["kind"] == "log_review" and item["status"] == "succeeded" for item in steps
            ):
                break
            time.sleep(3)
        role_steps = [item for item in steps if item["kind"] in ROLES]
        validation = next(
            (
                item.get("result", {}).get("experiment", {}).get("validation", {})
                for item in steps
                if item["kind"] == "backtest" and item.get("result")
            ),
            {},
        )
        execution = next((item.get("result") for item in steps if item["kind"] == "execute"), None)
        report = {
            "created_at": time.time(),
            "run_id": run_id,
            "elapsed_seconds": round(time.time() - started, 1),
            "passed": len(role_steps) == 6
            and all(item["status"] == "succeeded" for item in steps)
            and validation.get("status") == "completed"
            and bool(execution),
            "steps": [
                {
                    "kind": item["kind"],
                    "status": item["status"],
                    "attempts": item["attempts"],
                    "evidence_valid": (item.get("result") or {})
                    .get("evidence_validation", {})
                    .get("valid"),
                }
                for item in steps
            ],
            "registered_evidence_rate": sum(
                bool((item.get("result") or {}).get("evidence_validation", {}).get("valid"))
                for item in role_steps
            )
            / max(1, len(role_steps)),
            "native_engine": validation.get("engine"),
            "native_metrics": validation.get("metrics"),
            "strategy_verdict": validation.get("verdict"),
            "eligible_for_paper": validation.get("eligible_for_paper"),
            "execution": execution,
            "real_money_orders_sent": False,
            "limits": "One real Codex workflow; source membership is not factual entailment or forecast calibration.",
        }
        report["passed"] = report["passed"] and report["registered_evidence_rate"] == 1.0
        (state_root() / "prelaunch-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2), flush=True)
        raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
