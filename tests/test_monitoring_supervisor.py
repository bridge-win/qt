from __future__ import annotations

from pathlib import Path

from qt.monitoring.state import MonitorStateStore
from qt.monitoring.supervisor import run_supervised_loop


def test_supervised_loop_persists_stopped_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    seen: list[int] = []

    def tick(cycle: int) -> dict[str, object]:
        seen.append(cycle)
        return {"cycle": cycle, "ok": True}

    run_supervised_loop(
        name="test-loop",
        mode="paper",
        interval_seconds=1,
        cycles=2,
        state_path=state_path,
        tick=tick,
        sleep=lambda _: None,
    )

    snapshot = MonitorStateStore(state_path).read()
    assert seen == [1, 2]
    assert snapshot is not None
    assert snapshot.status == "stopped"
    assert snapshot.cycle == 2
    assert snapshot.consecutive_failures == 0


def test_supervised_loop_recovers_after_failure(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    seen: list[int] = []

    def tick(cycle: int) -> dict[str, object]:
        seen.append(cycle)
        if cycle == 1:
            raise RuntimeError("temporary failure")
        return {"cycle": cycle, "ok": True}

    run_supervised_loop(
        name="test-loop",
        mode="paper",
        interval_seconds=1,
        cycles=2,
        state_path=state_path,
        tick=tick,
        sleep=lambda _: None,
    )

    snapshot = MonitorStateStore(state_path).read()
    assert seen == [1, 2]
    assert snapshot is not None
    assert snapshot.status == "stopped"
    assert snapshot.consecutive_failures == 0
    assert snapshot.last_error is None
