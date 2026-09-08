"""Explicit resource admission policy for the constrained research node."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResourcePolicy:
    max_concurrent_jobs: int = 1
    max_queue_depth: int = 20
    max_estimated_rows: int = 1_000_000
    min_available_memory_mib: int = 512

    def admit(self, spec: Mapping[str, object], *, available_memory_mib: int | None) -> None:
        estimated_rows = _positive_int(spec.get("estimated_rows"), default=0)
        if estimated_rows > self.max_estimated_rows:
            raise CapacityBlockedError("estimated rows exceed this node's research cap; use a dedicated compute node")
        if available_memory_mib is not None and available_memory_mib < self.min_available_memory_mib:
            raise CapacityBlockedError("insufficient free memory on the research node; no job was submitted")


class CapacityBlockedError(ValueError):
    """A truthful admission result, distinct from failed research output."""


def available_memory_mib() -> int | None:
    """Return host-available RAM where Linux procfs is available."""

    try:
        values = dict(
            line.split(":", maxsplit=1)
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
            if ":" in line
        )
        return int(values["MemAvailable"].split()[0]) // 1024
    except (FileNotFoundError, KeyError, OSError, ValueError):
        return None


def _positive_int(value: object, *, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return max(0, int(str(value)))
    except ValueError:
        return default
