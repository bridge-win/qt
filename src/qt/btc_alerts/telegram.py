"""Verified private recipient and conservative Telegram transport outcomes."""

from __future__ import annotations

import re
from typing import Any

import httpx

from .models import digest
from .store import Store


class Telegram:
    def __init__(self, token: str, chat_id: str, expected_bot: str, client: httpx.Client | None = None):
        self._token, self._chat_id = token, chat_id
        self.expected_bot = expected_bot.removeprefix("@")
        self.client = client or httpx.Client(timeout=10, trust_env=False, follow_redirects=False)
        self.configured = bool(
            re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token)
            and re.fullmatch(r"[1-9][0-9]{3,19}", chat_id)
            and re.fullmatch(r"[A-Za-z0-9_]{5,32}", self.expected_bot)
        )
        self.recipient_key = digest({"chat_id": chat_id, "asset": "BTC"})
        self.binding = digest({"token": token, "chat_id": chat_id, "bot": self.expected_bot.casefold()})

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if method not in {"getMe", "getUpdates", "getWebhookInfo", "sendMessage"} or not self.configured:
            raise ValueError("telegram_not_configured")
        try:
            response = self.client.post(f"https://api.telegram.org/bot{self._token}/{method}", json=payload)
            if len(response.content) > 2_000_000:
                raise ValueError("oversized_response")
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("invalid_response")
            return response.status_code, value
        except Exception:
            # Never render credential-bearing request URLs or provider text.
            raise RuntimeError("telegram_transport_unknown") from None

    def verify(self, store: Store, nonce: str, now: float) -> dict[str, Any]:
        if not re.fullmatch(r"QT VERIFY [A-Za-z0-9_-]{16,64}", nonce):
            raise ValueError("invalid_verification_challenge")
        status, body = self.request("getMe", {})
        bot = body.get("result", {})
        if status != 200 or body.get("ok") is not True or bot.get("is_bot") is not True or str(bot.get("username", "")).casefold() != self.expected_bot.casefold():
            raise ValueError("telegram_bot_identity_mismatch")
        status, body = self.request("getWebhookInfo", {})
        if status != 200 or body.get("ok") is not True or body.get("result", {}).get("url"):
            raise ValueError("telegram_webhook_or_unavailable")
        status, body = self.request("getUpdates", {"timeout": 0, "limit": 100})
        if status != 200 or body.get("ok") is not True or not isinstance(body.get("result"), list):
            raise ValueError("telegram_discovery_unavailable")
        for update in body["result"]:
            message = update.get("message", {})
            chat, sender = message.get("chat", {}), message.get("from", {})
            if (
                message.get("text") == nonce and chat.get("type") == "private"
                and str(chat.get("id")) == self._chat_id and sender.get("id") == chat.get("id")
                and sender.get("is_bot") is False and 0 <= now - message.get("date", 0) <= 600
            ):
                receipt = {"verified": True, "binding": self.binding, "bot": self.expected_bot,
                           "verified_at": now, "challenge_hash": digest(nonce)}
                store.put("recipient_verification", receipt)
                return {"verified": True, "bot": self.expected_bot, "verified_at": now}
        raise ValueError("private_recipient_challenge_not_found")

    def verified(self, store: Store) -> bool:
        receipt = store.get("recipient_verification", {})
        return bool(self.configured and receipt.get("verified") and receipt.get("binding") == self.binding)

    def send(self, text: str) -> dict[str, Any]:
        if not 1 <= len(text) <= 4096:
            return {"status": "failed", "error_code": "invalid_message_size"}
        try:
            status, body = self.request("sendMessage", {"chat_id": self._chat_id, "text": text})
        except RuntimeError:
            return {"status": "unknown", "error_code": "transport_unknown"}
        if status == 429:
            delay = body.get("parameters", {}).get("retry_after", 1)
            if type(delay) is not int or not 1 <= delay <= 300:
                return {"status": "failed", "error_code": "rate_limit_horizon"}
            return {"status": "pending", "error_code": "rate_limited", "retry_after": delay}
        result = body.get("result", {})
        if (
            status == 200 and body.get("ok") is True and isinstance(result, dict)
            and type(result.get("message_id")) is int and result["message_id"] > 0
            and str(result.get("chat", {}).get("id")) == self._chat_id
        ):
            return {"status": "accepted", "message_id": result["message_id"]}
        if status >= 500 or 200 <= status < 300:
            return {"status": "unknown", "error_code": "telegram_response_unknown"}
        return {"status": "failed", "error_code": "telegram_rejected"}
