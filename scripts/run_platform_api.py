"""Run the QT control API as a dedicated process."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import uvicorn

from qt.platform.api import create_app
from qt.platform.config import PlatformSettings
from qt.platform.health import ExpectedWorker


@dataclass(frozen=True)
class ApiArguments:
    host: str
    port: int
    expected_workers: tuple[ExpectedWorker, ...]


def parse_args(argv: Sequence[str] | None = None) -> ApiArguments:
    parser = argparse.ArgumentParser(description="Run the QT control API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8876)
    parser.add_argument(
        "--expected-worker",
        action="append",
        default=[],
        metavar="ROLE:INSTANCE",
        type=_expected_worker,
        help="worker identity to monitor; repeat for each expected worker",
    )
    namespace = parser.parse_args(argv)
    expected_workers = tuple(cast(list[ExpectedWorker], namespace.expected_worker))
    duplicate = _find_duplicate(expected_workers)
    if duplicate is not None:
        parser.error(f"duplicate expected worker: {duplicate.role}:{duplicate.instance_id}")
    return ApiArguments(
        host=str(namespace.host),
        port=int(namespace.port),
        expected_workers=expected_workers,
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    settings = PlatformSettings()
    uvicorn.run(
        create_app(settings=settings, expected_workers=args.expected_workers),
        host=args.host,
        port=args.port,
    )


def _expected_worker(value: str) -> ExpectedWorker:
    if ":" not in value:
        raise argparse.ArgumentTypeError("expected worker must use ROLE:INSTANCE")
    if value.count(":") != 1:
        raise argparse.ArgumentTypeError("expected worker must contain exactly one ':'")
    role, instance_id = (part.strip() for part in value.split(":", maxsplit=1))
    if not role or not instance_id:
        raise argparse.ArgumentTypeError("expected worker requires non-empty role and instance")
    return ExpectedWorker(role=role, instance_id=instance_id)


def _find_duplicate(expected_workers: Sequence[ExpectedWorker]) -> ExpectedWorker | None:
    seen: set[tuple[str, str]] = set()
    for worker in expected_workers:
        identity = (worker.role, worker.instance_id)
        if identity in seen:
            return worker
        seen.add(identity)
    return None


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


if __name__ == "__main__":
    main()
