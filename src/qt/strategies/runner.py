"""Multi-strategy runner.

Runs every enabled strategy in its own thread, on its own cadence,
inside a single Python process. Each strategy writes a durable
heartbeat to ``<runtime_dir>/strategies/<name>.json`` that the
dashboard reads to render the per-strategy sub-routes.

Designed for the "one-line startup" use case: a single
``python scripts/run_all.py`` brings up every strategy + the
dashboard with no extra orchestration.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from qt.core.config import Settings
from qt.core.logging import get_logger
from qt.monitoring.alerts import alert
from qt.monitoring.state import MonitorStateStore, new_snapshot, with_update
from qt.strategies.base import Strategy

log = get_logger(__name__)


def strategy_state_dir(runtime_dir: str | Path) -> Path:
    """Where per-strategy heartbeat JSON files live."""
    p = Path(runtime_dir) / "strategies"
    p.mkdir(parents=True, exist_ok=True)
    return p


def strategy_state_path(runtime_dir: str | Path, strategy_name: str) -> Path:
    return strategy_state_dir(runtime_dir) / f"{strategy_name}.json"


def run_strategy_forever(
    strategy: Strategy,
    settings: Settings,
    *,
    runtime_dir: str | Path,
    stop_event: threading.Event,
    max_backoff_seconds: int = 300,
) -> None:
    """Drive a single strategy through repeated tick → sleep cycles.

    Persists a heartbeat on every cycle and fires ``alert(...)`` when
    the strategy returns a non-None Opportunity at or above
    ``config.min_alert_severity`` urgency.
    """

    state_path = strategy_state_path(runtime_dir, strategy.name)
    store = MonitorStateStore(state_path)
    snapshot = new_snapshot(
        name=strategy.name, mode=settings.execution.mode,
        details={"description": strategy.description},
    )
    store.write(snapshot)

    cycle = 0
    consecutive_failures = 0
    interval = max(1, int(strategy.config.interval_seconds))
    while not stop_event.is_set():
        cycle += 1
        try:
            data = strategy.fetch_data(settings)
            result = strategy.evaluate(data)
        except Exception as exc:
            consecutive_failures += 1
            backoff = min(max_backoff_seconds, interval * consecutive_failures)
            status = "failed" if consecutive_failures >= 5 else "degraded"
            snapshot = with_update(
                snapshot, status=status, cycle=cycle,
                consecutive_failures=consecutive_failures, last_error=str(exc),
            )
            store.write(snapshot)
            log.warning("strategy_cycle_failed", strategy=strategy.name, error=str(exc))
            alert(
                f"strategy {strategy.name} cycle failed",
                severity="warning",
                strategy=strategy.name, cycle=cycle, error=str(exc),
                backoff_seconds=backoff,
            )
            stop_event.wait(timeout=backoff)
            continue

        consecutive_failures = 0
        details: dict[str, object] = {
            "description": strategy.description,
            "params": strategy.config.params,
            "last_evaluation": result.as_dict(),
        }
        if result.opportunity is not None:
            alert(
                f"{strategy.name} {result.opportunity.action} opportunity: {result.opportunity.reason}",
                severity=strategy.config.min_alert_severity,
                strategy=strategy.name,
                **{k: v for k, v in result.opportunity.details.items()},
            )
            details["last_opportunity"] = result.opportunity.as_dict()
        elif "last_opportunity" in snapshot.details:
            details["last_opportunity"] = snapshot.details["last_opportunity"]
        snapshot = with_update(
            snapshot, status="healthy", cycle=cycle,
            consecutive_failures=0, last_error=None, details=details,
        )
        store.write(snapshot)
        stop_event.wait(timeout=interval)

    snapshot = with_update(snapshot, status="stopped", cycle=cycle)
    store.write(snapshot)


def start_all_strategies(
    strategies: list[Strategy],
    settings: Settings,
    *,
    runtime_dir: str | Path,
    stop_event: threading.Event,
) -> list[threading.Thread]:
    """Spawn one daemon thread per strategy. Returns the threads so the
    caller can ``.join`` on shutdown."""

    threads: list[threading.Thread] = []
    for s in strategies:
        t = threading.Thread(
            target=run_strategy_forever,
            kwargs={
                "strategy": s, "settings": settings,
                "runtime_dir": runtime_dir, "stop_event": stop_event,
            },
            name=f"strategy-{s.name}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info("strategy_started", strategy=s.name, interval=s.config.interval_seconds)
    return threads


def wait_for_shutdown(stop_event: threading.Event, poll: float = 1.0) -> None:
    """Block the main thread until ``stop_event`` is set."""

    try:
        while not stop_event.is_set():
            time.sleep(poll)
    except KeyboardInterrupt:
        stop_event.set()


__all__ = [
    "run_strategy_forever",
    "start_all_strategies",
    "strategy_state_dir",
    "strategy_state_path",
    "wait_for_shutdown",
]
