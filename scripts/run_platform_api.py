"""Run the QT control API as a dedicated process."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

import uvicorn

from qt.platform.api import create_app
from qt.platform.config import PlatformSettings


@dataclass(frozen=True)
class ApiArguments:
    host: str
    port: int


def parse_args(argv: Sequence[str] | None = None) -> ApiArguments:
    parser = argparse.ArgumentParser(description="Run the QT control API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8876)
    namespace = parser.parse_args(argv)
    return ApiArguments(host=str(namespace.host), port=int(namespace.port))


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    settings = PlatformSettings()
    uvicorn.run(create_app(settings=settings), host=args.host, port=args.port)


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


if __name__ == "__main__":
    main()
