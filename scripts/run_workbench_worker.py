"""Run the bounded, lease-aware QT workbench worker lifecycle."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from signal import SIGINT, SIGTERM, signal
from threading import Event
from time import monotonic

from qt.workbench.worker import WorkbenchWorker


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the QT research-only workbench worker")
    parser.add_argument("--parquet-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--worker-id", default="qt-workbench-worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run at most one FIFO job, for cron/manual recovery only",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="idle poll interval, bounded to 0.5..60 seconds",
    )
    parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=90.0,
        help="attempt heartbeat lease timeout, bounded to 30..3600 seconds",
    )
    args = parser.parse_args(argv)
    if not 0.5 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.5 and 60")
    if not 30 <= args.stale_after_seconds <= 3600:
        parser.error("--stale-after-seconds must be between 30 and 3600")
    worker = WorkbenchWorker(
        parquet_root=args.parquet_root,
        state_root=args.state_root,
        worker_id=args.worker_id,
    )
    stopped = Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal(SIGINT, stop)
    signal(SIGTERM, stop)
    while not stopped.is_set():
        cycle_started = monotonic()
        worker.research_repository.recover_stale(
            stale_after=timedelta(seconds=args.stale_after_seconds)
        )
        worker.research_repository.heartbeat(args.worker_id)
        ran_job = worker.run_next()
        if args.once:
            return
        remaining = args.poll_seconds - (monotonic() - cycle_started)
        # Keep a short bounded wait whether the preceding cycle had work or
        # merely renewed its lease; this is intentionally concurrency-one.
        stopped.wait(max(0.0, remaining if not ran_job else min(remaining, 0.5)))


if __name__ == "__main__":
    main()
