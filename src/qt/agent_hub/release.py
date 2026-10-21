"""Readiness evidence. Service liveness is never synonymous with release approval."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from qt.agent_hub.research import native_bridge_fingerprint


def code_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".html"}:
            digest.update(path.relative_to(Path(__file__).parent).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def release_fingerprint(runtime: Any) -> str:
    value = {
        "code_sha256": code_fingerprint(),
        "config": runtime.config.model_dump(),
        "skills": runtime.skills.list(),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def readiness(runtime: Any) -> dict[str, Any]:
    tenant = runtime.config.tenant_id
    now = time.time()
    checks = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(
            {"id": name, "status": "passed" if passed else "unverified", "detail": detail}
        )

    trading = runtime.trading.status(tenant)
    add("paper_mode", runtime.config.mode == "paper", "Automatic release scope is paper trading.")
    add("skills", len(runtime.skills.list()) == 6, "Six versioned server-owned role manifests.")
    add(
        "research_service",
        runtime.research is not None,
        "Configured native validation transport; execution checked separately.",
    )
    jobs = runtime.store.successful_jobs_between(
        tenant, list(runtime.config.roles), since=max(0, now - 86400), until=now
    )
    current = release_fingerprint(runtime)
    jobs = [
        item
        for item in jobs
        if item.get("result") and item["result"].get("release_sha256") == current
    ]
    reports = [
        item["result"].get("experiment", {}).get("validation", {})
        for item in jobs
        if item["status"] == "succeeded"
    ]
    current_engine = {}
    if runtime.research is not None and hasattr(runtime.research, "health"):
        with suppress(Exception):
            current_engine = runtime.research.health().get("engine", {})
    add(
        "native_validation",
        any(
            item.get("status") == "completed"
            and current_engine
            and item.get("engine") == current_engine
            and current_engine.get("native_bridge_sha256") == native_bridge_fingerprint()
            for item in reports
        ),
        "A native research report from this release must exist within 24h; completion does not imply profitable strategy.",
    )
    runs: dict[str, set[str]] = {}
    for item in jobs:
        if item["status"] == "succeeded":
            runs.setdefault(str(item["payload"].get("pipeline_id", item["id"])), set()).add(
                item["kind"]
            )
    add(
        "six_role_workflow",
        any(set(runtime.config.roles) <= roles for roles in runs.values()),
        "All six roles must finish in the same production workflow and release within 24h.",
    )
    add(
        "no_unresolved_orders",
        not trading.get("unresolved_orders", []),
        "Unknown orders require reconciliation before new risk.",
    )
    report = {}
    path = runtime.root / "evaluations" / "latest.json"
    if path.is_file():
        with suppress(ValueError, OSError):
            report = json.loads(path.read_text())
    add(
        "safety_evaluation",
        report.get("passed") is True
        and report.get("code_sha256") == code_fingerprint()
        and report.get("release_sha256") == current
        and 0 <= now - report.get("created_at", 0) < 86400,
        "Frozen adversarial scenarios use the production workflow in isolated paper ledgers; valid for 24h and this code.",
    )
    return {
        "version": 2,
        "mode": runtime.config.mode,
        "checks": checks,
        "paper_ready": all(item["status"] == "passed" for item in checks),
        "live_ready": False,
        "live_blockers": [
            "Real account permissions, venue eligibility and funded order lifecycle unverified",
            "No approved live strategy or forward performance/calibration acceptance dataset",
            "Cloud failover and multi-tenant security acceptance not performed",
        ],
        "evaluation": report,
    }
