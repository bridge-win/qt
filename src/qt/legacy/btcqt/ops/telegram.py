# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Telegram control channel: status, daily report, pause/resume, flatten, KILL.

Optional — if no token is configured the bot silently disables and alerts go
to the log only. Only the configured chat_id may issue commands.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


class Notifier:
    """Fallback notifier: logs. TgBot subclasses it when telegram is configured."""

    async def send(self, text: str) -> None:
        log.info("[ALERT] %s", text)

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class TgBot(Notifier):
    def __init__(self, token: str, chat_id: str, callbacks: dict[str, Callable[[], Awaitable[str]]]):
        from telegram.ext import ApplicationBuilder, CommandHandler
        self.chat_id = str(chat_id)
        self.callbacks = callbacks
        self.app = ApplicationBuilder().token(token).build()
        for cmd in ("status", "pause", "resume", "flatten", "kill", "help"):
            self.app.add_handler(CommandHandler(cmd, self._make_handler(cmd)))

    def _make_handler(self, cmd: str):
        async def handler(update, context):
            if str(update.effective_chat.id) != self.chat_id:
                log.warning("telegram: ignoring command from foreign chat %s",
                            update.effective_chat.id)
                return
            if cmd == "help":
                await update.message.reply_text(
                    "/status 状态  /pause 暂停开新仓  /resume 恢复\n"
                    "/flatten 全平仓+撤单  /kill 一键熔断（平仓+停机，需 /resume 恢复）")
                return
            cb = self.callbacks.get(cmd)
            if cb is None:
                await update.message.reply_text(f"unknown command {cmd}")
                return
            try:
                result = await cb()
                await update.message.reply_text(result or "done")
            except Exception as e:
                log.exception("telegram command %s failed", cmd)
                await update.message.reply_text(f"error: {e}")
        return handler

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram bot started")
        await self.send("btc-qt 已启动 ✅")

    async def stop(self) -> None:
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception:
            log.exception("telegram stop failed")

    async def send(self, text: str) -> None:
        try:
            await self.app.bot.send_message(chat_id=self.chat_id, text=text[:4000])
        except Exception:
            log.exception("telegram send failed; message was: %s", text[:200])


def build_notifier(token: str, chat_id: str, callbacks: dict) -> Notifier:
    if token and chat_id:
        try:
            return TgBot(token, chat_id, callbacks)
        except Exception:
            log.exception("telegram init failed — falling back to log notifier")
    else:
        log.info("telegram not configured — alerts go to log only")
    return Notifier()


