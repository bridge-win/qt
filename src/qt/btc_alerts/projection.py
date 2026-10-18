"""Readiness projection and optimistic policy changes without HTTP dependencies."""

from __future__ import annotations

from typing import Any

from .knowledge import strategy_cards
from .models import Policy
from .store import Store


def projection(store: Store, now: float) -> dict[str, Any]:
    revision, policy = store.policy()
    worker = store.get("worker_status", {})
    alive = (
        0 <= now - store.get("heartbeat", 0) <= 20
        and 0 <= now - store.get("cycle_started", 0) <= 240
    )
    reviewer = store.get("reviewer_status", {})
    reviewer["authenticated"] = bool(reviewer.get("authenticated") and 0 <= now - reviewer.get("at", 0) <= 86400)
    worker = {**worker, "reviewer": reviewer}
    if not alive:
        worker = {**worker, "status": "worker_unavailable", "markets": {}, "recipient_verified": False}
    worker = {**worker, "alive": alive}
    validations = [
        {"strategy_id": card["strategy_id"], "direction": direction,
         **store.validation(policy.strategy_version, card["strategy_id"], direction)}
        for card in strategy_cards() for direction in ("long", "reduce_long", "short")
    ]
    return {
        "configured": True, "server_time": now, "revision": revision, "policy": policy.model_dump(),
        "strategy_version": policy.strategy_version, "worker": worker,
        "cards": strategy_cards(), "validations": validations, "events": store.latest_events(),
        "live_execution_enabled": False,
        "readiness": {
            "software": alive,
            "model": bool(reviewer["authenticated"]),
            "recipient": bool(worker.get("recipient_verified")),
            "strategies": any(value.get("status") == "passed" for value in validations),
        },
    }


def update_policy(store: Store, expected_revision: int, policy: Policy, now: float) -> int:
    _, current = store.policy()
    if current.strategy_version != policy.strategy_version:
        policy = policy.model_copy(update={"mode": "shadow"})
    if policy.mode == "notify":
        worker = projection(store, now)["worker"]
        reviewer = store.get("reviewer_status", {})
        if (
            not worker.get("alive") or not worker.get("recipient_verified")
            or not reviewer.get("authenticated") or not reviewer.get("model_pinned")
            or not 0 <= now - reviewer.get("at", 0) <= 86400
        ):
            raise ValueError("notification_readiness_pending")
        eligible = False
        for event in store.latest_events(500):
            if not event["payload"].get("is_test"):
                from .models import Candidate

                candidate = Candidate.model_validate(event["payload"])
                if candidate.strategy_version == policy.strategy_version and not store.promotion_reasons(candidate, now):
                    eligible = True
                    break
        if not eligible:
            raise ValueError("strategy_validation_or_forward_shadow_pending")
    return store.set_policy(expected_revision, policy, now)
