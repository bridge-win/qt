"""HTTP boundary to the isolated numerical executor; never execute code here.

The API service only submits a whitelisted numeric backtest. The executor must
run as a separate non-root container with read-only rootfs, bounded writable
tmpfs, CPU/memory/PID limits, dropped capabilities and no external network.
An HTTP timeout is not evidence that a job was cancelled: retain the same job ID
for operator reconciliation instead of retrying a side effect under a new ID.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from qt.agent_hub.context import identifier
from qt.agent_hub.tracing import traceparent


def validate_request(request: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "task",
        "prices",
        "candles",
        "initial_cash",
        "fee_bps",
        "slippage_bps",
        "window",
        "strategy",
    }
    if not isinstance(request, dict) or set(request) - allowed or request.get("task") != "backtest":
        raise ValueError("sandbox supports only the predefined numeric backtest")
    strategy = request.get("strategy", "moving_average")
    if strategy not in ("moving_average", "btc_capitulation"):
        raise ValueError("unsupported backtest strategy")
    prices = request.get("prices", [])
    if not isinstance(prices, list) or len(prices) > 10000:
        raise ValueError("backtest supports at most 10000 prices")
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        for value in prices
    ):
        raise ValueError("prices must be positive finite numbers")
    normalized = {
        "task": "backtest",
        "strategy": strategy,
        "prices": [float(value) for value in prices],
    }
    for key, lower, upper in (
        ("initial_cash", 0.01, 1e6),
        ("fee_bps", 0, 1000),
        ("slippage_bps", 0, 1000),
    ):
        value = request.get(key, 1000 if key == "initial_cash" else 10)
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not lower <= value <= upper
        ):
            raise ValueError("invalid backtest parameter")
        normalized[key] = float(value)
    window = request.get("window", 20)
    if type(window) is not int or not 2 <= window <= 500:
        raise ValueError("invalid backtest window")
    normalized["window"] = window
    if strategy == "moving_average" and len(prices) < window + 2:
        raise ValueError("backtest prices require sufficient warmup")
    candles = request.get("candles", [])
    if not isinstance(candles, list) or len(candles) > 2000:
        raise ValueError("invalid OHLCV series")
    for row in candles:
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError("invalid OHLCV shape")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in row):
            raise ValueError("OHLCV values must be finite numbers")
    if strategy == "btc_capitulation" and len(candles) < 210:
        raise ValueError("BTC backtest requires at least 210 candles")
    normalized["candles"] = [[float(value) for value in row] for row in candles]
    encoded = json.dumps(normalized, allow_nan=False).encode()
    if len(encoded) > 262144:
        raise ValueError("sandbox request exceeds the configured limit")
    return normalized


class SandboxRunner:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.url = os.environ.get("QT_AGENT_HUB_SANDBOX_URL", "").rstrip("/")
        self.token = os.environ.get(
            "QT_AGENT_HUB_EXECUTOR_TOKEN", os.environ.get("QT_AGENT_HUB_SERVICE_TOKEN", "")
        )

    def run(self, tenant_id: str, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        tenant_id, job_id = identifier(tenant_id), identifier(job_id)
        clean = validate_request(request)
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
            or not self.token
        ):
            return {
                "status": "unavailable",
                "reason": "isolated_executor_not_configured",
                "job_id": job_id,
            }
        try:
            propagation = traceparent()
            with (
                httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client,
                client.stream(
                    "POST",
                    self.url + "/run",
                    json=clean,
                    headers={
                        "Authorization": "Bearer " + self.token,
                        **({"traceparent": propagation} if propagation else {}),
                    },
                ) as response,
            ):
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 524288:
                        raise ValueError("executor response too large")
            result = json.loads(data)
            if not isinstance(result, dict) or result.get("input_hash") != request_hash(clean):
                raise ValueError("executor returned invalid result")
            if clean["strategy"] == "moving_average":
                if not isinstance(result.get("metrics"), dict) or not isinstance(
                    result.get("equity_curve"), list
                ):
                    raise ValueError("executor returned invalid result")
                if not isinstance(result.get("fills"), list):
                    raise ValueError("executor returned invalid result")
            elif not isinstance(result.get("report"), dict):
                raise ValueError("executor returned invalid report")
            # Round-trip JSON validation rejects NaN/Infinity in an untrusted executor response.
            json.dumps(result, allow_nan=False)
            result.update({"status": "completed", "tenant_id": tenant_id, "job_id": job_id})
            return result
        except (httpx.HTTPError, ValueError, UnicodeError):
            return {"status": "failed", "reason": "isolated_executor_failed", "job_id": job_id}


def request_hash(request: dict[str, Any]) -> str:
    """Canonical input identity shared with the fixed executor implementation."""
    return hashlib.sha256(json.dumps(request, sort_keys=True, allow_nan=False).encode()).hexdigest()
