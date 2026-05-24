"""Small supervised loop for long-running paper/live processes."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qt.monitoring.alerts import alert
from qt.monitoring.state import MonitorStateStore, MonitorStatus, new_snapshot, with_update

Tick = Callable[[int], Mapping[str, object] | None]
Sleep = Callable[[float], None]


def run_supervised_loop(
    *,
    name: str,
    mode: str,
    interval_seconds: int,
    state_path: str | Path,
    tick: Tick,
    cycles: int = 0,
    max_backoff_seconds: int = 300,
    sleep: Sleep = time.sleep,
) -> None:
    """Run `tick` forever with heartbeat persistence and bounded backoff."""

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if cycles < 0:
        raise ValueError("cycles must be zero or positive")

    store = MonitorStateStore(state_path)
    snapshot = new_snapshot(name=name, mode=mode)
    store.write(snapshot)

    cycle = 0
    consecutive_failures = 0
    while cycles == 0 or cycle < cycles:
        cycle += 1
        try:
            details = dict(tick(cycle) or {})
        except Exception as exc:
            consecutive_failures += 1
            backoff = min(max_backoff_seconds, interval_seconds * consecutive_failures)
            next_run_at = datetime.now(tz=timezone.utc) + timedelta(seconds=backoff)
            status: MonitorStatus = "failed" if consecutive_failures >= 5 else "degraded"
            snapshot = with_update(
                snapshot,
                status=status,
                cycle=cycle,
                consecutive_failures=consecutive_failures,
                last_error=str(exc),
                next_run_at=next_run_at,
            )
            store.write(snapshot)
            alert(
                "supervised cycle failed",
                severity="critical" if status == "failed" else "warning",
                name=name,
                cycle=cycle,
                error=str(exc),
                backoff_seconds=backoff,
            )
            sleep(backoff)
            continue

        consecutive_failures = 0
        next_run_at = datetime.now(tz=timezone.utc) + timedelta(seconds=interval_seconds)
        snapshot = with_update(
            snapshot,
            status="healthy",
            cycle=cycle,
            consecutive_failures=0,
            last_error=None,
            next_run_at=next_run_at,
            details=details,
        )
        store.write(snapshot)
        if cycles == 0 or cycle < cycles:
            sleep(interval_seconds)

    snapshot = with_update(snapshot, status="stopped", cycle=cycle, next_run_at=None)
    store.write(snapshot)
