"""Structured logging setup. JSON in production, pretty console in dev."""

from __future__ import annotations

import logging
import sys
from threading import RLock
from typing import cast

import structlog


class _CurrentStderrLogger:
    """A structlog-compatible printer that never retains a redirected stderr."""

    _lock = RLock()

    def __init__(self, *_args: object) -> None:
        pass

    def msg(self, message: str) -> None:
        with self._lock:
            print(message, file=sys.stderr, flush=True)

    log = debug = info = warn = warning = msg
    fatal = failure = err = error = critical = exception = msg


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
    ]
    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        context_class=dict,
        # CLI tests (and embedded callers) can replace sys.stderr temporarily.
        # The factory resolves it at emission time, so cached bound loggers never
        # retain a closed Click capture stream.
        logger_factory=_CurrentStderrLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
