"""Read-only, bounded projection reader for the existing authenticated workbench."""

from __future__ import annotations

import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Response
from pydantic import ValidationError

from qt.btc_monitor.models import MonitorSnapshot

MAX_SNAPSHOT_BYTES = 1024 * 1024
SNAPSHOT_MAX_AGE_SECONDS = 10.0
FUTURE_TOLERANCE_SECONDS = 2.0


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_tree(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for child in value.values():
            _finite_tree(child)
    elif isinstance(value, list):
        for child in value:
            _finite_tree(child)


def _read_snapshot(path: Path) -> MonitorSnapshot:
    # Do not create directories, open SQLite, follow symlinks, or block on a FIFO.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("invalid snapshot size or file type")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot too large")
        value = json.loads(raw, parse_constant=_reject_constant)
        _finite_tree(value)
        return MonitorSnapshot.model_validate(value)
    finally:
        os.close(descriptor)


def read_projection(state_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """Recheck freshness on every request; disk history never grants execution."""
    now = time.time() if now is None else now
    response: dict[str, Any] = {
        "snapshot": None,
        "read_status": "unavailable",
        "read_reasons": [],
        "server_time": now,
        "snapshot_age_seconds": None,
        "snapshot_max_age_seconds": SNAPSHOT_MAX_AGE_SECONDS,
        "future_tolerance_seconds": FUTURE_TOLERANCE_SECONDS,
        "valid_until": None,
        "data_valid_until": None,
        "live_execution_enabled": False,
    }
    try:
        snapshot = _read_snapshot(state_dir / "latest.json")
    except FileNotFoundError:
        response["read_reasons"] = ["snapshot_missing"]
        return response
    except (OSError, ValueError, ValidationError, RecursionError, UnicodeError):
        # Never return parser/OS exception text, state paths or untrusted file content.
        response["read_reasons"] = ["snapshot_invalid"]
        return response

    configured_limits = snapshot.context.get("freshness_limits", {})
    limits = configured_limits if isinstance(configured_limits, dict) else {}
    future_tolerance = limits.get("max_future_seconds", FUTURE_TOLERANCE_SECONDS)
    if (
        not isinstance(future_tolerance, (int, float))
        or not math.isfinite(future_tolerance)
        or future_tolerance < 0
    ):
        future_tolerance = FUTURE_TOLERANCE_SECONDS
    future_tolerance = min(float(future_tolerance), FUTURE_TOLERANCE_SECONDS)
    response["future_tolerance_seconds"] = future_tolerance
    reasons: list[str] = []
    age = now - snapshot.published_at
    response["snapshot_age_seconds"] = max(0.0, age)
    valid_until = min(
        snapshot.published_at + SNAPSHOT_MAX_AGE_SECONDS,
        snapshot.assessment.as_of + SNAPSHOT_MAX_AGE_SECONDS,
        snapshot.assessment.expires_at,
    )
    if age < -future_tolerance:
        reasons.append("snapshot_future")
    elif age > SNAPSHOT_MAX_AGE_SECONDS:
        reasons.append("snapshot_stale")
    if snapshot.published_at < 0:
        reasons.append("snapshot_invalid_time")
    if snapshot.assessment.as_of > now + future_tolerance:
        reasons.append("assessment_future")
    if snapshot.assessment.expires_at <= now:
        reasons.append("assessment_expired")
    if snapshot.assessment.as_of + SNAPSHOT_MAX_AGE_SECONDS < now:
        reasons.append("assessment_stale")
    if snapshot.assessment.expires_at < snapshot.assessment.as_of:
        reasons.append("assessment_invalid_time")
    if not snapshot.health.alive:
        reasons.append("worker_not_alive")
    progress = snapshot.health.last_progress_at
    if progress is None or now - progress > 30 or progress > now + future_tolerance:
        snapshot.health.alive = False
        reasons.append("worker_progress_stale")
    for stamp in (snapshot.health.last_persisted_at,):
        if stamp is not None and stamp > now + future_tolerance:
            reasons.append("persistence_future")

    data_valid_until = snapshot.published_at + SNAPSHOT_MAX_AGE_SECONDS
    if progress is not None:
        data_valid_until = min(data_valid_until, progress + 30)
    defaults = {"trade": 5.0, "quote": 5.0, "candle": 125.0}
    backlog_limit = limits.get("max_backlog_seconds", 2.0)
    if (
        not isinstance(backlog_limit, (int, float))
        or not math.isfinite(backlog_limit)
        or backlog_limit <= 0
    ):
        backlog_limit = 2.0
    backlog_limit = min(float(backlog_limit), 2.0)
    for name, default in defaults.items():
        channel = snapshot.health.channels.get(name)
        if channel is None:
            reasons.append(f"{name}_missing")
            continue
        limit = limits.get(f"{name}_stale_seconds", default)
        if not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
            limit = default
        # A projection cannot loosen the read side's documented safety defaults.
        limit = min(float(limit), default)
        channel.receive_age_seconds = (
            None if channel.last_receive_at is None else max(0.0, now - channel.last_receive_at)
        )
        channel.event_age_seconds = (
            None if channel.last_event_at is None else max(0.0, now - channel.last_event_at)
        )
        stamps = [channel.last_receive_at]
        if channel.source_time_known:
            stamps.append(channel.last_event_at)
        stale = any(stamp is None or now - stamp > limit for stamp in stamps)
        backlog = channel.backlog_seconds
        backlog_bad = backlog is None or backlog > backlog_limit or backlog < 0
        if name != "quote" and not channel.source_time_known:
            stale = True
        future = any(stamp is not None and stamp > now + future_tolerance for stamp in stamps)
        if stale or future or backlog_bad or not channel.ready:
            channel.ready = False
            reasons.append(f"{name}_unready")
        for stamp in stamps:
            if stamp is not None:
                valid_until = min(valid_until, stamp + limit)
                data_valid_until = min(data_valid_until, stamp + limit)
    if not snapshot.health.data_ready:
        reasons.append("source_not_ready")
    if not snapshot.health.assessment_ready:
        reasons.append("assessment_not_ready")
    assessment_only = {
        "assessment_future",
        "assessment_expired",
        "assessment_stale",
        "assessment_invalid_time",
        "assessment_not_ready",
    }
    source_failed = any(reason not in assessment_only for reason in reasons)
    if reasons:
        if any(reason not in assessment_only for reason in reasons):
            snapshot.health.data_ready = False
        snapshot.health.assessment_ready = False
        snapshot.health.status = "degraded"
        snapshot.health.reasons = list(dict.fromkeys([*snapshot.health.reasons, *reasons]))
        if snapshot.assessment.state == "CANDIDATE":
            snapshot.assessment.state = "DATA_UNHEALTHY"
            snapshot.assessment.reasons = list(
                dict.fromkeys([*reasons, *snapshot.assessment.reasons])
            )
    for event in snapshot.recent_events:
        if event.state in {"WATCH", "CONFIRMED"} and (
            source_failed
            or event.expires_at <= now
            or event.as_of > now + future_tolerance
            or age > SNAPSHOT_MAX_AGE_SECONDS
            or age < -future_tolerance
        ):
            event.state = "EXPIRED"
            event.reason = "read_side_expired"
    response.update(
        snapshot=snapshot.model_dump(mode="json"),
        read_status="degraded" if reasons else "ok",
        read_reasons=list(dict.fromkeys(reasons)),
        valid_until=min(valid_until, data_valid_until),
        data_valid_until=data_valid_until,
    )
    return response


def build_btc_monitor_router(state_dir: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/btc-monitor")
    def btc_monitor(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return read_projection(state_dir)

    return router
