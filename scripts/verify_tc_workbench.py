"""Verify an installed TC workbench through its authenticated HTTP API.

Run on TC from /opt/qt. Creates explicitly named deployment experiments and
retains their reports for inspection in the web application.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
from dotenv import dotenv_values


def main() -> None:
    env = dotenv_values(".env.workbench")
    headers = {
        "cf-access-client-id": str(env["QT_WORKBENCH_ORIGIN_CLIENT_ID"]),
        "cf-access-client-secret": str(env["QT_WORKBENCH_ORIGIN_CLIENT_SECRET"]),
        "x-qt-access-subject": "tc-deployment-verification",
    }
    with httpx.Client(base_url="http://127.0.0.1:8877", headers=headers, timeout=30) as client:
        def request(method: str, path: str, payload: object = None) -> dict:
            response = client.request(
                method, "/api/v3" + path, json=payload,
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            response.raise_for_status()
            return response.json()

        def wait(job: dict) -> dict:
            job_id = job["job"]["job_id"]
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                response = request("GET", "/jobs/" + job_id)
                current = response.get("job", response)
                if current["status"] in {"complete", "completed", "succeeded", "failed", "cancelled"}:
                    if current["status"] not in {"complete", "completed", "succeeded"}:
                        raise RuntimeError(json.dumps(current))
                    return current
                time.sleep(2)
            raise TimeoutError(job_id)

        version = request("POST", "/strategies", {
            "name": "TC deployment verification: weekly DCA",
            "base_strategy_id": "qt:fixed_dca",
        })["version"]["version_id"]
        queued = request("POST", "/experiments", {
            "strategy_version_id": version,
            "dataset_id": "okx-btcusdt-1h",
            "from": "2026-01-01T00:00:00Z",
            "to": "2026-02-01T00:00:00Z",
            "costs": {"initial_cash": 10000, "fee_bps": 10},
        })
        first = wait(queued)
        print(json.dumps({"first_job": first["job_id"], "status": first["status"]}), flush=True)
        second = wait(request("POST", "/experiments/" + first["job_id"] + "/reproduce"))
        assert first["result"]["metrics"] == second["result"]["metrics"], "Reproduction metrics differ"
        print(json.dumps({"reproduced_job": second["job_id"], "metrics": second["result"]["metrics"]}), flush=True)
        print(json.dumps({"runtime": request("GET", "/runtime")}), flush=True)


if __name__ == "__main__":
    main()
