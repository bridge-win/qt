"""Run the durable single-concurrency BTC research worker."""

from __future__ import annotations

import argparse
import os
import socket
import time
from datetime import timedelta
from pathlib import Path

from qt.research.datasets import DatasetSynchronizer
from qt.research.executor import ResearchExecutor
from qt.research.repository import ResearchRepository
from qt.research.worker import (
    Cancelled,
    Progress,
    ResearchCancelledError,
    ResearchWorker,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parquet-dir", default="data/parquet")
    parser.add_argument("--backtests-dir", default="data/backtests")
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    args = parser.parse_args()
    if args.poll_seconds <= 0 or args.poll_seconds > 30:
        parser.error("--poll-seconds must be between 0 and 30")

    parquet_dir = Path(args.parquet_dir)
    backtests_dir = Path(args.backtests_dir)
    repository = ResearchRepository(backtests_dir / "research.sqlite3")
    executor = ResearchExecutor(parquet_dir, backtests_dir)
    synchronizer = DatasetSynchronizer(parquet_dir)

    def execute(
        spec: dict[str, object],
        progress: Progress,
        cancelled: Cancelled,
    ) -> dict[str, object]:
        if spec.get("kind") == "dataset_sync":
            progress("dataset_sync", 20)
            if cancelled():
                raise ResearchCancelledError
            dataset = synchronizer.sync(str(spec["dataset_id"]))
            progress("dataset_validation", 90)
            return {"kind": "dataset_sync", "dataset": dataset}
        return executor.execute(spec, progress, cancelled)

    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    worker = ResearchWorker(
        repository,
        worker_id=worker_id,
        executor=execute,
    )
    repository.recover_stale(stale_after=timedelta(minutes=5))
    last_heartbeat = 0.0
    while True:
        now = time.monotonic()
        if now - last_heartbeat >= 5:
            repository.heartbeat(worker_id)
            last_heartbeat = now
        if not worker.run_once():
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
