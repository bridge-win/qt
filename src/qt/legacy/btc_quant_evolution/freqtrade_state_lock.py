# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterator


@dataclass
class _LockState:
    gate: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0
    handle: IO[str] | None = None


_REGISTRY_GUARD = threading.Lock()
_REGISTRY_PID = os.getpid()
_LOCK_STATES: dict[Path, _LockState] = {}


@contextmanager
def freqtrade_state_lock(root_dir: Path) -> Iterator[None]:
    lock_path = root_dir.resolve() / "user_data" / "research_runs" / ".freqtrade-state.lock"
    state = _lock_state(lock_path)
    with state.gate:
        if state.depth == 0:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                handle.close()
                raise
            state.handle = handle
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0:
                handle = state.handle
                state.handle = None
                if handle is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()


def _lock_state(lock_path: Path) -> _LockState:
    global _REGISTRY_PID
    with _REGISTRY_GUARD:
        current_pid = os.getpid()
        if current_pid != _REGISTRY_PID:
            _LOCK_STATES.clear()
            _REGISTRY_PID = current_pid
        return _LOCK_STATES.setdefault(lock_path, _LockState())

