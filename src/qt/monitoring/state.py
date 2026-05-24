"""Durable monitor heartbeat state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

MonitorStatus = Literal["starting", "healthy", "degraded", "failed", "stopped"]


@dataclass(frozen=True)
class MonitorSnapshot:
    name: str
    mode: str
    status: MonitorStatus
    started_at: str
    updated_at: str
    cycle: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    next_run_at: str | None = None
    details: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class MonitorStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> MonitorSnapshot | None:
        if not self.path.exists():
            return None
        with self.path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            return None
        return MonitorSnapshot(
            name=str(payload.get("name", "")),
            mode=str(payload.get("mode", "")),
            status=_coerce_status(payload.get("status")),
            started_at=str(payload.get("started_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            cycle=int(payload.get("cycle", 0)),
            consecutive_failures=int(payload.get("consecutive_failures", 0)),
            last_error=_coerce_optional_str(payload.get("last_error")),
            next_run_at=_coerce_optional_str(payload.get("next_run_at")),
            details=_coerce_details(payload.get("details")),
        )

    def write(self, snapshot: MonitorSnapshot) -> None:
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(snapshot.as_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")
        tmp.replace(self.path)


def new_snapshot(
    *,
    name: str,
    mode: str,
    status: MonitorStatus = "starting",
    details: Mapping[str, object] | None = None,
) -> MonitorSnapshot:
    now = datetime.now(tz=timezone.utc).isoformat()
    return MonitorSnapshot(
        name=name,
        mode=mode,
        status=status,
        started_at=now,
        updated_at=now,
        details=dict(details or {}),
    )


def with_update(
    snapshot: MonitorSnapshot,
    *,
    status: MonitorStatus,
    cycle: int | None = None,
    consecutive_failures: int | None = None,
    last_error: str | None = None,
    next_run_at: datetime | None = None,
    details: Mapping[str, object] | None = None,
) -> MonitorSnapshot:
    return MonitorSnapshot(
        name=snapshot.name,
        mode=snapshot.mode,
        status=status,
        started_at=snapshot.started_at,
        updated_at=datetime.now(tz=timezone.utc).isoformat(),
        cycle=snapshot.cycle if cycle is None else cycle,
        consecutive_failures=(
            snapshot.consecutive_failures
            if consecutive_failures is None
            else consecutive_failures
        ),
        last_error=last_error,
        next_run_at=next_run_at.isoformat() if next_run_at else None,
        details=dict(snapshot.details if details is None else details),
    )


def _coerce_status(value: object) -> MonitorStatus:
    if value in {"starting", "healthy", "degraded", "failed", "stopped"}:
        return value  # type: ignore[return-value]
    return "failed"


def _coerce_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _coerce_details(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items()}
