# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Optional Alternative.me Fear & Greed poller (display context only)."""
from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger(__name__)


async def poll_fng(queue: asyncio.Queue):
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get("https://api.alternative.me/fng/?limit=1") as resp:
                    if resp.status == 200:
                        body = await resp.json(content_type=None)
                        rows = body.get("data") or []
                        if rows:
                            await queue.put(("fng", float(rows[0]["value"])))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("F&G poll failed: %s", exc)
            await asyncio.sleep(900)


