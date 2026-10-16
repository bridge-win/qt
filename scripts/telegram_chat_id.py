#!/usr/bin/env python3
"""Discover Telegram chat IDs locally without putting a bot token in shell history.

Run this yourself after rotating any token that was shared in chat. This helper
only reads getMe/getWebhookInfo/getUpdates. It never sends a message, changes a webhook,
sets an update offset, saves the token, or prints raw Telegram responses/errors.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import re
import sys
import warnings
from typing import Any
from urllib.parse import urlencode


class DiscoveryError(Exception):
    """A fixed, safe-to-display error without HTTP paths or response bodies."""


def api_call(token: str, method: str) -> Any:
    """Use HTTPS directly; never expose request paths in errors or redirects."""
    if method not in {"getMe", "getWebhookInfo", "getUpdates"}:
        raise DiscoveryError("Unsupported discovery method.")
    connection = http.client.HTTPSConnection("api.telegram.org", timeout=15)
    payload = urlencode({"timeout": 0, "limit": 100}) if method == "getUpdates" else ""
    try:
        connection.request(
            "POST",
            f"/bot{token}/{method}",
            body=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        status = response.status
        if status == 409:
            raise DiscoveryError(
                "Another bot poller or webhook is active. Stop its polling yourself before retrying."
            )
        if status in {401, 404}:
            raise DiscoveryError("Telegram rejected the token. Check the newly generated token locally.")
        if status == 429:
            raise DiscoveryError("Telegram rate limited this request. Wait before retrying.")
        if status != 200:
            raise DiscoveryError("Telegram discovery failed. No webhook or messages were changed.")
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise DiscoveryError("Telegram returned an oversized response. Nothing was printed.")
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("ok") is not True:
            code = data.get("error_code") if isinstance(data, dict) else None
            if code == 409:
                raise DiscoveryError(
                    "Another bot poller or webhook is active. Stop its polling yourself before retrying."
                )
            raise DiscoveryError("Telegram discovery failed. Check the token and bot status locally.")
        return data.get("result")
    except DiscoveryError:
        raise
    except Exception:
        # HTTP/TLS/JSON errors can contain the credential-bearing request path.
        raise DiscoveryError("Cannot reach or decode Telegram securely. Check your network and retry.") from None
    finally:
        connection.close()


def verify_bot(token: str, expected_bot: str | None = None) -> str:
    expected = expected_bot.removeprefix("@") if expected_bot is not None else None
    if expected is not None and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", expected):
        raise DiscoveryError("Expected bot must be a Telegram username, optionally prefixed with @.")
    bot = api_call(token, "getMe")
    username = bot.get("username") if isinstance(bot, dict) else None
    if (
        not isinstance(bot, dict)
        or bot.get("is_bot") is not True
        or not isinstance(username, str)
        or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
    ):
        raise DiscoveryError("Telegram returned an invalid bot identity. No chats were queried.")
    if expected is not None and username.casefold() != expected.casefold():
        raise DiscoveryError("The token belongs to a different bot. No chats were queried.")
    return username


def discover(token: str) -> list[dict[str, str]]:
    webhook = api_call(token, "getWebhookInfo")
    if not isinstance(webhook, dict) or "url" not in webhook:
        raise DiscoveryError("Telegram returned an invalid webhook status. Nothing was changed.")
    if webhook["url"]:
        raise DiscoveryError(
            "This bot has a webhook. Use its existing receiver to find the chat ID; "
            "this helper will not remove or change it."
        )
    updates = api_call(token, "getUpdates")
    if not isinstance(updates, list):
        raise DiscoveryError("Telegram returned invalid updates. Nothing was printed.")
    chats: dict[str, dict[str, str]] = {}
    for update in updates:
        if not isinstance(update, dict):
            continue
        for field in ("message", "edited_message", "channel_post", "edited_channel_post"):
            message = update.get(field)
            chat = message.get("chat") if isinstance(message, dict) else None
            if not isinstance(chat, dict):
                continue
            chat_id, chat_type = chat.get("id"), chat.get("type")
            if type(chat_id) is not int or chat_type not in {
                "private", "group", "supergroup", "channel"
            }:
                continue
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            label = label[:120] if isinstance(label, str) else ""
            chats[str(chat_id)] = {"chat_id": str(chat_id), "type": chat_type, "label": label}
    return list(chats.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-bot", metavar="USERNAME", help="Require this public bot username before discovering chats.")
    args = parser.parse_args(argv)
    print(
        "First open your bot in Telegram, press Start and send a short message. "
        "Enter a newly generated token below; input is hidden and is not saved.",
        file=sys.stderr,
    )
    try:
        if not sys.stdin.isatty():
            raise DiscoveryError("Run this helper in an interactive terminal for hidden token entry.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("New Telegram bot token (hidden): ").strip()
        if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token):
            raise DiscoveryError("The token format is invalid. Paste the new token only at the hidden prompt.")
        username = verify_bot(token, args.expected_bot)
        print(f"Verified sender: @{username}", file=sys.stderr)
        chats = discover(token)
        if not chats:
            raise DiscoveryError(
                "No chat found. Press Start, send a new message to this bot, then rerun. "
                "If another receiver consumes updates, use that receiver's chat ID."
            )
        # JSON escaping prevents a chat label from injecting terminal controls.
        print(json.dumps(chats, ensure_ascii=True))
        print(
            "Choose the intended recipient yourself. Put its chat_id in QT_TELEGRAM_CHAT_ID "
            "in your private deployment file. No message was sent.",
            file=sys.stderr,
        )
        return 0
    except (EOFError, KeyboardInterrupt):
        print("Discovery cancelled.", file=sys.stderr)
        return 1
    except (DiscoveryError, getpass.GetPassWarning) as exc:
        message = str(exc) if isinstance(exc, DiscoveryError) else "Hidden token entry is unavailable. Use an interactive terminal."
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
