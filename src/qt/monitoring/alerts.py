"""Operational alerts.

Supports three sinks, all optional and independent:

* ``stderr`` (always on) — structured log via :mod:`qt.core.logging`.
* ``email`` — SMTP, configured via ``QT_SMTP_*`` env vars.
* ``telegram`` — Bot API, configured via ``QT_TELEGRAM_*`` env vars.

Sinks that are not fully configured are silently skipped, so the
function is safe to call from any environment. Notification failures
never raise — they log a warning and continue, because losing an alert
must never crash the trading loop.

Environment variables
---------------------
QT_SMTP_HOST, QT_SMTP_PORT (default 465), QT_SMTP_USER, QT_SMTP_PASSWORD,
QT_SMTP_FROM (default = USER), QT_SMTP_TO (comma-separated),
QT_SMTP_USE_SSL (default "true").

QT_TELEGRAM_BOT_TOKEN, QT_TELEGRAM_CHAT_ID.

QT_ALERT_MIN_SEVERITY (default "info") — filter floor; one of
``info|warning|critical``.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Literal

from qt.core.logging import get_logger

log = get_logger(__name__)

Severity = Literal["info", "warning", "critical"]
_SEVERITY_ORDER: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}


def alert(message: str, *, severity: Severity = "info", **context: object) -> None:
    """Emit an alert to every configured sink. Never raises."""
    fn = {"info": log.info, "warning": log.warning, "critical": log.error}[severity]
    fn("alert", message=message, **context)

    floor = os.getenv("QT_ALERT_MIN_SEVERITY", "info").lower()
    if _SEVERITY_ORDER.get(severity, 0) < _SEVERITY_ORDER.get(floor, 0):
        return

    subject = f"[QT {severity.upper()}] {message}"
    body_lines = [message, ""]
    body_lines.extend(f"{k}: {v}" for k, v in context.items())
    body = "\n".join(body_lines)

    _send_email_safe(subject, body)
    _send_telegram_safe(subject, body)


def _send_email_safe(subject: str, body: str) -> None:
    host = os.getenv("QT_SMTP_HOST")
    user = os.getenv("QT_SMTP_USER")
    password = os.getenv("QT_SMTP_PASSWORD")
    to_raw = os.getenv("QT_SMTP_TO") or user
    if not (host and user and password and to_raw):
        return
    recipients = [a.strip() for a in to_raw.split(",") if a.strip()]
    if not recipients:
        return
    port = int(os.getenv("QT_SMTP_PORT", "465"))
    use_ssl = os.getenv("QT_SMTP_USE_SSL", "true").lower() in {"1", "true", "yes"}
    sender = os.getenv("QT_SMTP_FROM") or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        if use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.send_message(msg)
    except Exception as exc:
        log.warning("alert_email_failed", error=str(exc))


def _send_telegram_safe(subject: str, body: str) -> None:
    token = os.getenv("QT_TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("QT_TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    text = f"{subject}\n\n{body}"
    if len(text) > 3900:
        text = text[:3900] + "\n…(truncated)"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as exc:
        log.warning("alert_telegram_failed", error=str(exc))
