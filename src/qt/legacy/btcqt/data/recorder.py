# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Buffered append-only Parquet journal partitioned by UTC day and event kind."""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


def event_to_row(kind: str, ev):
    mapping = {
        "candle": "klines", "liq": "forceorder_binance", "mark": "mark",
        "oi": "oi", "funding_settled": "funding", "book": "book",
    }
    table = mapping.get(kind)
    if table is None:
        return None
    if is_dataclass(ev):
        row = asdict(ev)
    elif isinstance(ev, dict):
        row = dict(ev)
    else:
        return None
    row.setdefault("ts", int(time.time() * 1000))
    return table, row


class Recorder:
    def __init__(self, root: Path, flush_rows: int = 250, flush_sec: float = 30.0):
        self.root = Path(root) / "live"
        self.flush_rows, self.flush_sec = flush_rows, flush_sec
        self.buffers: dict[tuple[str, str], list[dict]] = {}
        self._seq = 0
        self._last_flush = time.monotonic()

    def add(self, table: str, row: dict):
        ts = int(row.get("ts") or time.time() * 1000)
        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        key = (day, table)
        self.buffers.setdefault(key, []).append(dict(row))
        if sum(map(len, self.buffers.values())) >= self.flush_rows or \
                time.monotonic() - self._last_flush >= self.flush_sec:
            self.flush()

    def latest_ts(self, table: str) -> int | None:
        """Read the newest persisted timestamp without loading the full journal."""
        if not self.root.exists():
            return None
        for day in sorted((path for path in self.root.iterdir() if path.is_dir()),
                          reverse=True):
            latest = None
            for path in day.glob(f"{table}-*.parquet"):
                try:
                    frame = pd.read_parquet(path, columns=["ts"])
                except Exception as exc:
                    log.warning("skip unreadable %s: %s", path, exc)
                    continue
                if not frame.empty:
                    value = int(frame["ts"].max())
                    latest = value if latest is None else max(latest, value)
            if latest is not None:
                return latest
        return None

    def append_many(self, table: str, rows: Iterable[dict]) -> int:
        """Persist a bounded backfill efficiently as one atomic file per UTC day."""
        partitions: dict[str, list[dict]] = {}
        for source in rows:
            row = dict(source)
            ts = int(row.get("ts") or time.time() * 1000)
            day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            partitions.setdefault(day, []).append(row)
        written = 0
        for day, partition in sorted(partitions.items()):
            if not partition:
                continue
            dest = self.root / day
            dest.mkdir(parents=True, exist_ok=True)
            self._seq += 1
            stamp = int(time.time() * 1000)
            path = dest / f"{table}-backfill-{stamp}-{self._seq}.parquet"
            tmp = path.with_suffix(".tmp.parquet")
            pd.DataFrame(partition).to_parquet(tmp, index=False)
            tmp.replace(path)
            written += len(partition)
        return written

    def flush(self):
        for (day, table), rows in list(self.buffers.items()):
            if not rows:
                continue
            dest = self.root / day
            dest.mkdir(parents=True, exist_ok=True)
            self._seq += 1
            path = dest / f"{table}-{int(time.time() * 1000)}-{self._seq}.parquet"
            tmp = path.with_suffix(".tmp.parquet")
            pd.DataFrame(rows).to_parquet(tmp, index=False)
            tmp.replace(path)
            self.buffers[(day, table)] = []
        self._last_flush = time.monotonic()

