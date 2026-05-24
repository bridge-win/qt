from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from qt.monitoring.health import evaluate_monitor_health
from qt.monitoring.state import MonitorStateStore, new_snapshot, with_update


def test_monitor_health_missing_state_is_unhealthy(tmp_path: Path) -> None:
    health = evaluate_monitor_health(
        tmp_path / "missing.json",
        stale_after_seconds=60,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert health.ok is False
    assert health.status == "missing"


def test_monitor_health_detects_healthy_snapshot(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "state.json"
    snapshot = new_snapshot(name="paper", mode="paper", status="healthy")
    MonitorStateStore(path).write(snapshot)

    health = evaluate_monitor_health(path, stale_after_seconds=60, now=now)

    assert health.ok is True
    assert health.status == "healthy"


def test_monitor_health_detects_stale_snapshot(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "state.json"
    snapshot = new_snapshot(name="paper", mode="paper", status="healthy")
    snapshot = with_update(
        snapshot,
        status="healthy",
        details={"score": 0.0},
    )
    old_updated = (now - timedelta(minutes=10)).isoformat()
    stale_snapshot = snapshot.__class__(
        name=snapshot.name,
        mode=snapshot.mode,
        status=snapshot.status,
        started_at=snapshot.started_at,
        updated_at=old_updated,
        cycle=snapshot.cycle,
        consecutive_failures=snapshot.consecutive_failures,
        last_error=snapshot.last_error,
        next_run_at=snapshot.next_run_at,
        details=snapshot.details,
    )
    MonitorStateStore(path).write(stale_snapshot)

    health = evaluate_monitor_health(path, stale_after_seconds=60, now=now)

    assert health.ok is False
    assert health.status == "stale"


def test_monitor_health_detects_failed_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    snapshot = new_snapshot(name="paper", mode="paper", status="failed")
    MonitorStateStore(path).write(snapshot)

    health = evaluate_monitor_health(
        path,
        stale_after_seconds=60,
        now=datetime.now(tz=timezone.utc),
    )

    assert health.ok is False
    assert health.status == "failed"
