"""Protected workbench projection and small durable control requests."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from .models import PolicyUpdate, TestNotificationRequest
from .projection import projection, update_policy
from .store import ConflictError, Store


def build_alerts_router(root: Path | None) -> APIRouter:
    router = APIRouter()
    store = Store(root) if root is not None else None

    def required() -> Store:
        if store is None:
            raise HTTPException(503, detail="btc_alerts_not_configured_on_this_host")
        return store

    @router.get("/btc-alerts")
    def status() -> dict[str, Any]:
        if store is None:
            return {"configured": False, "live_execution_enabled": False, "reason": "btc_alerts_not_configured_on_this_host"}
        return projection(store, time.time())

    @router.get("/btc-alerts/events/{event_id}")
    def event(event_id: str) -> dict[str, Any]:
        value = required().event(event_id)
        if value is None:
            raise HTTPException(404, detail="alert_event_not_found")
        return value

    @router.put("/btc-alerts/policy")
    def policy(request: PolicyUpdate) -> dict[str, Any]:
        try:
            update_policy(required(), request.expected_revision, request.policy, time.time())
        except ConflictError as error:
            raise HTTPException(409, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(409, detail=str(error)) from None
        return projection(required(), time.time())

    @router.post("/btc-alerts/test-notification", status_code=202)
    def test_notification(request: TestNotificationRequest) -> dict[str, Any]:
        value = projection(required(), time.time())
        if value["policy"]["mode"] == "off" or not value["readiness"]["recipient"] or not value["worker"]["alive"]:
            raise HTTPException(409, detail="verified_recipient_and_running_worker_required")
        event_id = required().request_test(request.request_id, time.time(), value["strategy_version"])
        return {"event_id": event_id, "status": "queued", "is_test": True}

    return router
