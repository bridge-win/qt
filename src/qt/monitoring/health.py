"""Health checks over the durable monitor heartbeat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from qt.monitoring.state import MonitorSnapshot, MonitorStateStore


@dataclass(frozen=True)
class MonitorHealth:
    status: str
    ok: bool
    message: str
    state_path: str
    snapshot_status: str | None = None
    updated_at: str | None = None
    age_seconds: float | None = None
    consecutive_failures: int | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "ok": self.ok,
            "message": self.message,
            "state_path": self.state_path,
            "snapshot_status": self.snapshot_status,
            "updated_at": self.updated_at,
            "age_seconds": self.age_seconds,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }


def evaluate_monitor_health(
    state_path: str | Path,
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> MonitorHealth:
    """Evaluate heartbeat health for automation and watchdogs."""

    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")

    path = Path(state_path)
    snapshot = MonitorStateStore(path).read()
    if snapshot is None:
        return MonitorHealth(
            status="missing",
            ok=False,
            message="monitor state file is missing or unreadable",
            state_path=str(path),
        )

    age = _age_seconds(snapshot.updated_at, now or datetime.now(tz=timezone.utc))
    if age is None:
        return _from_snapshot(
            snapshot,
            status="invalid",
            ok=False,
            message="monitor updated_at timestamp is invalid",
            state_path=path,
            age_seconds=age,
        )
    if age > stale_after_seconds:
        return _from_snapshot(
            snapshot,
            status="stale",
            ok=False,
            message=f"monitor heartbeat is stale by {age:.0f}s",
            state_path=path,
            age_seconds=age,
        )
    if snapshot.status in {"failed", "degraded"}:
        return _from_snapshot(
            snapshot,
            status=snapshot.status,
            ok=False,
            message=f"monitor status is {snapshot.status}",
            state_path=path,
            age_seconds=age,
        )
    if snapshot.status == "stopped":
        return _from_snapshot(
            snapshot,
            status="stopped",
            ok=False,
            message="monitor loop has stopped",
            state_path=path,
            age_seconds=age,
        )
    return _from_snapshot(
        snapshot,
        status="healthy",
        ok=True,
        message="monitor heartbeat is healthy",
        state_path=path,
        age_seconds=age,
    )


def _from_snapshot(
    snapshot: MonitorSnapshot,
    *,
    status: str,
    ok: bool,
    message: str,
    state_path: Path,
    age_seconds: float | None,
) -> MonitorHealth:
    return MonitorHealth(
        status=status,
        ok=ok,
        message=message,
        state_path=str(state_path),
        snapshot_status=snapshot.status,
        updated_at=snapshot.updated_at,
        age_seconds=age_seconds,
        consecutive_failures=snapshot.consecutive_failures,
        last_error=snapshot.last_error,
    )


def _age_seconds(updated_at: str, now: datetime) -> float | None:
    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds())
