"""Versioned research evidence. Search never grants access to raw holdout files."""

from __future__ import annotations

import json
import time

from pydantic import Field
from sqlalchemy import select

from qt.contracts import Contract, content_hash
from qt.contracts.base import NonEmptyStr, Sha256
from qt.persistence import Conflict, Repository
from qt.persistence.schema import artifacts


class EvidenceDocument(Contract):
    title: NonEmptyStr
    source: NonEmptyStr
    text: str = Field(strict=True, min_length=1, max_length=128_000)
    source_hash: Sha256
    published_at_ns: int
    available_at_ns: int
    received_at_ns: int
    revision_id: NonEmptyStr
    rights: NonEmptyStr


class EvidenceRepository:
    def __init__(self, repository: Repository):
        self.repository = repository

    def ingest(self, tenant: str, document: EvidenceDocument) -> dict:
        now = time.time_ns()
        if (not 0 <= document.published_at_ns <= document.available_at_ns <= document.received_at_ns <= now
                or len(document.text.encode()) > 128_000):
            raise ValueError("evidence_time_or_size_invalid")
        metadata = document.model_dump(mode="json", exclude={"text"})
        if len(json.dumps(metadata, allow_nan=False).encode()) > 8_000:
            raise ValueError("evidence_metadata_exceeds_tool_budget")
        return self.repository.put(tenant, "evidence", document.model_dump(mode="json"))

    def read(self, tenant: str, identity: str, *, asof_ns: int,
             offset: int = 0, length: int = 2000) -> dict:
        """Read at most 3000 characters, with a hash-bound document/offset receipt."""
        if type(asof_ns) is not int or not 0 <= asof_ns <= time.time_ns():
            raise ValueError("invalid_evidence_knowledge_time")
        row = self.repository.get(tenant, "evidence", identity)
        if max(row["created_ns"], row["payload"]["received_at_ns"], row["payload"]["available_at_ns"]) > asof_ns:
            raise KeyError("evidence_not_yet_observable")
        text = row["payload"]["text"]
        if (type(offset) is not int or not 0 <= offset <= len(text)
                or type(length) is not int or not 1 <= length <= 3000):
            raise ValueError("bounded_evidence_excerpt_required")
        excerpt = text[offset:offset + length]
        end = offset + len(excerpt)
        result = {
            "id": identity, "sha256": row["sha256"], **row["payload"], "text": excerpt,
            "recorded_at_ns": row["created_ns"], "knowledge_time_ns": asof_ns,
            "offset": offset, "next_offset": end if end < len(text) else None,
            "total_characters": len(text), "is_excerpt": offset != 0 or end < len(text),
            "excerpt_sha256": content_hash({"document_sha256": row["sha256"], "offset": offset, "text": excerpt}),
        }
        if len(json.dumps(result, allow_nan=False).encode()) > 30_000:
            raise ValueError("evidence_excerpt_metadata_exceeds_tool_budget")
        return result

    def search(self, tenant: str, query: str, *, asof_ns: int, limit: int = 5) -> dict:
        if not query.strip() or len(query) > 200 or not 1 <= limit <= 10:
            raise ValueError("bounded_evidence_query_required")
        # A bounded lexical index over authority records, not embeddings that
        # silently mix document revisions. FTS can be a rebuildable accelerator.
        with self.repository.transaction() as db:
            rows = db.execute(select(artifacts.c.entity_id, artifacts.c.payload, artifacts.c.sha256).where(
                artifacts.c.tenant_id == tenant, artifacts.c.kind == "evidence",
                artifacts.c.created_ns <= asof_ns,
            ).order_by(artifacts.c.created_ns.desc(), artifacts.c.entity_id).limit(1000)).mappings().all()
        terms = query.casefold().split()[:16]
        ranked = []
        for row in rows:
            payload = row["payload"]
            if content_hash(payload) != row["sha256"]:
                raise Conflict("evidence_integrity_failure")
            if max(payload["available_at_ns"], payload["received_at_ns"]) > asof_ns:
                continue
            text = (payload["title"] + " " + payload["text"]).casefold()
            score = sum(text.count(term) for term in terms)
            if score:
                ranked.append((score, row["entity_id"]))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return {"query_hash": content_hash(query), "asof_ns": asof_ns,
                "items": [{"id": identity, "score": score} for score, identity in ranked[:limit]],
                "search_window": "latest_1000_visible_records", "method": "lexical_v1"}
