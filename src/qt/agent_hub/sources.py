"""Bounded public RSS evidence collection outside the model execution process."""

from __future__ import annotations

import hashlib
import json
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from qt.agent_hub.context import ContextStore

FEEDS = (
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
)


def collect(context: ContextStore, tenant: str, cache_root: Path) -> dict:
    """Persist headlines, dates and links, never claim to have read full articles."""
    cache_root.mkdir(parents=True, exist_ok=True)
    notes, failures = [], []
    for url in FEEDS:
        try:
            with (
                httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client,
                client.stream("GET", url) as response,
            ):
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 524288:
                        raise ValueError("feed_size_limit")
            tree = ET.fromstring(body)
            for item in tree.findall(".//item")[:8]:
                title = (item.findtext("title") or "").strip()[:600]
                link = (item.findtext("link") or "").strip()[:1800]
                published = (item.findtext("pubDate") or "").strip()[:100]
                if not title or not link.startswith("https://"):
                    continue
                stamp = parsedate_to_datetime(published).timestamp() if published else None
                if stamp is not None and stamp > time.time() + 300:
                    continue
                content = json.dumps(
                    {
                        "title": title,
                        "published": published,
                        "url": link,
                        "evidence_type": "RSS headline only; article body not retrieved",
                    },
                    ensure_ascii=False,
                )
                cache = cache_root / (hashlib.sha256(link.encode()).hexdigest() + ".json")
                if cache.exists():
                    notes.append(json.loads(cache.read_text()))
                    continue
                document = context.ingest(tenant, link, content, kind="public_feed")
                note = {
                    "id": document["id"],
                    "source": link,
                    "title": title,
                    "published": published,
                    "retrieved_at": time.time(),
                    "headline_only": True,
                }
                cache.write_text(json.dumps(note))
                notes.append(note)
        except (httpx.HTTPError, ValueError, ET.ParseError, OSError, OverflowError, TypeError):
            failures.append({"source": url, "status": "unavailable"})
    return {"items": notes, "failures": failures, "retrieved_at": time.time()}
