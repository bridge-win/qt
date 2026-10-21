"""Release acceptance evidence survives monitoring traffic and expires explicitly."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from qt.agent_hub.config import ROLES, HubConfig, Role
from qt.agent_hub.release import code_fingerprint, readiness, release_fingerprint
from qt.agent_hub.research import native_bridge_fingerprint
from qt.agent_hub.runtime import HubRuntime


class ResearchHealth:
    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "engine": {"fixture": True, "native_bridge_sha256": native_bridge_fingerprint()},
        }


@pytest.fixture
def runtime(tmp_path: Path) -> HubRuntime:
    config = HubConfig(roles={role: Role(instruction="test") for role in ROLES})
    return HubRuntime(config, tmp_path / "state", tmp_path / "files", research=ResearchHealth())


def record_roles(runtime: HubRuntime, when: float, *, release: str | None = None) -> None:
    tenant = runtime.config.tenant_id
    current = release or release_fingerprint(runtime)
    with patch("qt.agent_hub.store._now", return_value=when):
        for kind in ROLES:
            job = runtime.store.enqueue(
                tenant, kind, {"pipeline_id": "accepted-run"}, idempotency_key=kind
            )
            claimed = runtime.store.claim(tenant, "test", kinds=[kind])
            assert claimed
            result = {"release_sha256": current}
            if kind == "backtest":
                result["experiment"] = {
                    "validation": {
                        "status": "completed",
                        "engine": runtime.research.health()["engine"],
                    }
                }
            assert runtime.store.complete_and_enqueue(
                tenant, job["id"], "test", claimed["lease_token"], result
            )


def record_evaluation(runtime: HubRuntime, when: float) -> None:
    directory = runtime.root / "evaluations"
    directory.mkdir()
    (directory / "latest.json").write_text(
        json.dumps(
            {
                "passed": True,
                "created_at": when,
                "code_sha256": code_fingerprint(),
                "release_sha256": release_fingerprint(runtime),
            }
        )
    )


def test_acceptance_is_not_evicted_by_more_than_200_monitor_jobs(runtime: HubRuntime) -> None:
    now = time.time()
    record_roles(runtime, now - 3600)
    record_evaluation(runtime, now - 60)
    for number in range(220):
        runtime.store.enqueue("personal", "monitor", {}, idempotency_key=f"monitor-{number}")
    assert {job["kind"] for job in runtime.store.list_jobs("personal", 200)} == {"monitor"}
    report = readiness(runtime)
    assert report["paper_ready"], report["checks"]


@pytest.mark.parametrize("age,accepted", [(86400, True), (86400.01, False), (-1, False)])
def test_acceptance_requires_all_roles_inside_explicit_24h_window(
    runtime: HubRuntime, age: float, accepted: bool
) -> None:
    now = time.time()
    record_roles(runtime, now - age)
    record_evaluation(runtime, now - 60)
    with patch("qt.agent_hub.release.time.time", return_value=now):
        report = readiness(runtime)
    checks = {check["id"]: check["status"] for check in report["checks"]}
    expected = "passed" if accepted else "unverified"
    assert checks["native_validation"] == expected
    assert checks["six_role_workflow"] == expected
    assert report["paper_ready"] is accepted


def test_acceptance_does_not_reuse_another_release_or_future_evaluation(
    runtime: HubRuntime,
) -> None:
    now = time.time()
    record_roles(runtime, now - 60, release="old-release")
    record_evaluation(runtime, now + 3600)
    report = readiness(runtime)
    checks = {check["id"]: check["status"] for check in report["checks"]}
    assert checks["native_validation"] == "unverified"
    assert checks["six_role_workflow"] == "unverified"
    assert checks["safety_evaluation"] == "unverified"
    assert not report["paper_ready"]
