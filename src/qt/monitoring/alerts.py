"""Operational alerts. Stub: prints to stderr; plug in Slack/Telegram for prod."""

from __future__ import annotations

from typing import Literal

from qt.core.logging import get_logger

log = get_logger(__name__)

Severity = Literal["info", "warning", "critical"]


def alert(message: str, *, severity: Severity = "info", **context: object) -> None:
    fn = {"info": log.info, "warning": log.warning, "critical": log.error}[severity]
    fn("alert", message=message, **context)
