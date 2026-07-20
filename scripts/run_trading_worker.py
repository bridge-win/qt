"""Run the QT trading worker as a dedicated process."""

from __future__ import annotations

import argparse
import math
import re
import signal
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import TypeAlias

from qt import __version__
from qt.platform.commands import CommandRepository
from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, create_session_factory
from qt.platform.operations import OperationsRepository
from qt.platform.worker import TradingWorker

_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SignalHandler: TypeAlias = Callable[[int, FrameType | None], object] | int | None


@dataclass(frozen=True)
class WorkerArguments:
    worker_id: str
    poll_seconds: float
    once: bool


def parse_args(argv: Sequence[str] | None = None) -> WorkerArguments:
    parser = argparse.ArgumentParser(description="Run the QT trading worker")
    parser.add_argument("--worker-id", required=True, type=_worker_id)
    parser.add_argument("--poll-seconds", default=1.0, type=_poll_seconds)
    parser.add_argument("--once", action="store_true")
    namespace = parser.parse_args(argv)
    return WorkerArguments(
        worker_id=str(namespace.worker_id),
        poll_seconds=float(namespace.poll_seconds),
        once=bool(namespace.once),
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    settings = PlatformSettings()
    engine = create_platform_engine(settings)
    try:
        session_factory = create_session_factory(engine)
        worker = TradingWorker(
            CommandRepository(session_factory),
            OperationsRepository(session_factory),
            worker_id=args.worker_id,
            version=__version__,
            command_lease_seconds=settings.command_lease_seconds,
            poll_seconds=args.poll_seconds,
        )
        with _stop_signal_handlers(worker):
            if args.once:
                worker.run_once()
            else:
                worker.run_forever()
    finally:
        engine.dispose()


@contextmanager
def _stop_signal_handlers(worker: TradingWorker) -> Iterator[None]:
    previous: dict[signal.Signals, SignalHandler] = {}

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        worker.stop()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _worker_id(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("worker id must not be blank")
    if _WORKER_ID.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError(
            "worker id must contain only letters, numbers, '.', '_', or '-'"
        )
    return normalized


def _poll_seconds(value: str) -> float:
    try:
        poll_seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("poll seconds must be a number") from error
    if not math.isfinite(poll_seconds):
        raise argparse.ArgumentTypeError("poll seconds must be finite")
    if poll_seconds <= 0:
        raise argparse.ArgumentTypeError("poll seconds must be greater than 0")
    if poll_seconds > 300:
        raise argparse.ArgumentTypeError("poll seconds must be at most 300")
    return poll_seconds


if __name__ == "__main__":
    main()
