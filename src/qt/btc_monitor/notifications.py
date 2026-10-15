"""Optional async Telegram outbox consumer, with at-most-once unknown outcomes.

A durable ``sending`` claim precedes each request. Timeouts, transport failures,
and process death can occur after server acceptance; these become terminal
``unknown`` rather than being retried. Only explicit Telegram 429 rejection is
retried. ``accepted`` proves API acceptance, not delivery to a user's device.
No response bodies, credentials, request URLs, or raw exceptions are logged.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any

import httpx
from pydantic import SecretStr

from .store import SnapshotStore, timestamp


class _TelegramLogRedaction(logging.Filter):
    """httpx INFO logs otherwise include the bot credential in the URL."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = re.sub(
            r"(https://api\.telegram\.org/bot)[^/\s]+(/)",
            r"\1[REDACTED]\2",
            message,
        )
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


class AsyncTelegram:
    def __init__(
        self,
        store: SnapshotStore,
        token: Any,
        chat_id: Any,
        *,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
        max_retry_after: float = 3600,
        poll_interval: float = 1.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_attempts < 1 or max_retry_after <= 0 or poll_interval <= 0:
            raise ValueError("invalid_notification_bounds")
        httpx_logger = logging.getLogger("httpx")
        if not any(isinstance(item, _TelegramLogRedaction) for item in httpx_logger.filters):
            httpx_logger.addFilter(_TelegramLogRedaction())
        self._token = SecretStr(self._secret(token))
        self._chat_id = SecretStr(self._secret(chat_id))
        if not self._token.get_secret_value() or not self._chat_id.get_secret_value():
            raise ValueError("telegram_credentials_missing")
        self._store = store
        self._client = client
        self._owns_client = client is None
        self._max_attempts = max_attempts
        self._max_retry_after = max_retry_after
        self._poll_interval = poll_interval
        self._clock = clock
        self._stopping = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._active_run = False

    @staticmethod
    def _secret(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "get_secret_value"):
            return str(value.get_secret_value())
        return str(value)

    async def _update(self, item: Mapping[str, Any], status: str, **kwargs: Any) -> None:
        await self._store.update_outbox(
            item["event_id"],
            item["revision"],
            item["notification_class"],
            status=status,
            attempts=kwargs.pop("attempts", item["attempts"]),
            **kwargs,
        )

    async def run_once(self) -> int:
        """Process a bounded due batch. Requests within one sender are serialized."""
        async with self._run_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(10, connect=5),
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                    follow_redirects=False,
                )
            items = await self._store.fetch_outbox(limit=16, now=self._clock())
            attempted = 0
            for item in items:
                if self._stopping.is_set():
                    break
                if not await self._store.notification_is_current(item):
                    await self._update(item, "obsolete")
                    continue
                if item["attempts"] >= self._max_attempts:
                    await self._update(item, "failed", error_code="attempt_limit")
                    continue
                attempts = item["attempts"] + 1
                # Durable before the network boundary. Restarts never retry a
                # sending claim whose external outcome cannot be established.
                if not await self._store.claim_outbox(item):
                    continue
                attempted += 1
                try:
                    response = await self._client.post(
                        f"https://api.telegram.org/bot{self._token.get_secret_value()}/sendMessage",
                        json={
                            "chat_id": self._chat_id.get_secret_value(),
                            "text": item["payload"]["text"],
                        },
                    )
                except asyncio.CancelledError:
                    # Leave the durable claim for restart recovery. Cancellation
                    # must not be interpreted as proof that Telegram rejected it.
                    raise
                except Exception:
                    await self._update(
                        item, "unknown", attempts=attempts, error_code="transport_unknown"
                    )
                    continue
                try:
                    body = response.json()
                except (ValueError, TypeError):
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                if response.status_code == 429:
                    delay = self._retry_delay(response, body)
                    retry_at = self._clock() + delay
                    if attempts >= self._max_attempts:
                        await self._update(
                            item, "failed", attempts=attempts, error_code="attempt_limit"
                        )
                    elif delay > self._max_retry_after:
                        await self._update(
                            item, "failed", attempts=attempts, error_code="rate_limit_horizon"
                        )
                    elif retry_at >= timestamp(item["expires_at"]):
                        await self._update(
                            item, "expired", attempts=attempts, error_code="rate_limited"
                        )
                    else:
                        await self._update(
                            item,
                            "pending",
                            attempts=attempts,
                            next_retry_at=retry_at,
                            error_code="rate_limited",
                        )
                elif 200 <= response.status_code < 300 and body.get("ok") is True:
                    await self._update(item, "accepted", attempts=attempts)
                elif response.status_code >= 500 or 200 <= response.status_code < 300:
                    await self._update(
                        item, "unknown", attempts=attempts, error_code="telegram_invalid_response"
                    )
                else:
                    await self._update(
                        item, "failed", attempts=attempts, error_code="telegram_rejected"
                    )
            return attempted

    @staticmethod
    def _retry_delay(response: httpx.Response, body: Mapping[str, Any]) -> float:
        values = [1.0]
        parameters = body.get("parameters", {})
        candidates = [response.headers.get("retry-after")]
        if isinstance(parameters, dict):
            candidates.append(parameters.get("retry_after"))
        for value in candidates:
            try:
                delay = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(delay) and delay >= 0:
                values.append(delay)
        return max(values)

    async def run(self) -> None:
        self._active_run = True
        try:
            while not self._stopping.is_set():
                await self.run_once()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)
        finally:
            self._active_run = False
            await self.aclose()

    async def stop(self) -> None:
        self._stopping.set()
        if not self._active_run:
            await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


AsyncTelegramSender = AsyncTelegram
