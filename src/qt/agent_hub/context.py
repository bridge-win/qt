"""Tenant-scoped SQLite retrieval and expiring memory without a model service."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MAX_DOCUMENT_CHARS = 1_000_000
MAX_MEMORY_BYTES = 32_768
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")


def identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid tenant or job identifier")
    return value


def _timestamp(
    value: str | datetime | float | None, *, default: float | None = None
) -> float | None:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid ISO timestamp") from None
    if isinstance(value, datetime):
        value = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid timestamp")
    return float(value)


def _source(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or "\x00" in value:
        raise ValueError("invalid source")
    if value.startswith(("/", "\\")) or "\\" in value or ".." in value.split("/"):
        raise ValueError("source path traversal is forbidden")
    return value


def _safe_path(root: Path, *parts: str) -> Path:
    current = root
    for part in parts:
        for segment in Path(part).parts:
            if segment in ("", ".", "..") or Path(segment).is_absolute():
                raise ValueError("invalid relative file path")
            current /= segment
            if current.is_symlink():
                raise ValueError("symlink paths are forbidden")
    if not current.resolve().is_relative_to(root):
        raise ValueError("file path escapes the controlled root")
    return current


class ContextStore:
    def __init__(self, path: Path, files_root: Path) -> None:
        self.path = Path(path)
        if self.path.is_symlink() or Path(files_root).is_symlink():
            raise ValueError("context roots cannot be symlinks")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.files_root = Path(files_root).resolve()
        self.files_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS hub_documents (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, source TEXT NOT NULL,
                    kind TEXT NOT NULL, content TEXT NOT NULL, reference_path TEXT NOT NULL,
                    created_at REAL NOT NULL, valid_from REAL NOT NULL, valid_until REAL,
                    sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS hub_documents_scope
                    ON hub_documents(tenant_id,valid_from,valid_until);
                CREATE TABLE IF NOT EXISTS hub_memories (
                    tenant_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    scope TEXT NOT NULL, source TEXT, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, expires_at REAL,
                    PRIMARY KEY (tenant_id,key)
                );
                """
            )
            try:
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS hub_document_fts USING fts5("
                    "doc_id UNINDEXED, tenant_id UNINDEXED, content, tokenize='unicode61')"
                )
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False
        self.path.chmod(0o600)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(
        self,
        tenant_id: str,
        source: str,
        content: str | None,
        *,
        kind: str = "knowledge",
        valid_from: str | datetime | float | None = None,
        valid_until: str | datetime | float | None = None,
    ) -> dict:
        tenant_id, source, kind = identifier(tenant_id), _source(source), identifier(kind)
        if content is None:
            if not source.startswith("mount:"):
                raise ValueError("file imports require a mount: source")
            return self.import_file(
                tenant_id,
                source[6:],
                kind=kind,
                valid_from=valid_from,
                valid_until=valid_until,
            )
        if not isinstance(content, str) or not 1 <= len(content) <= MAX_DOCUMENT_CHARS:
            raise ValueError("document is empty or exceeds the configured limit")
        now = time.time()
        start, end = _timestamp(valid_from, default=now), _timestamp(valid_until)
        if end is not None and end <= start:
            raise ValueError("valid_until must follow valid_from")
        document_id = uuid.uuid4().hex
        reference = _safe_path(self.files_root, tenant_id, "documents", document_id + ".txt")
        reference.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        reference.write_text(content, encoding="utf-8")
        reference.chmod(0o600)
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self._db() as db:
            db.execute(
                "INSERT INTO hub_documents VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    document_id,
                    tenant_id,
                    source,
                    kind,
                    content,
                    str(reference),
                    now,
                    start,
                    end,
                    digest,
                ),
            )
            if self._fts:
                db.execute(
                    "INSERT INTO hub_document_fts VALUES (?,?,?)", (document_id, tenant_id, content)
                )
        return {
            "id": document_id,
            "tenant_id": tenant_id,
            "source": source,
            "kind": kind,
            "reference_path": str(reference),
            "sha256": digest,
            "created_at": now,
            "valid_from": start,
            "valid_until": end,
            "content_chars": len(content),
        }

    def import_file(self, tenant_id: str, relative_path: str, **metadata: object) -> dict:
        """Import UTF-8 from <files_root>/<tenant>/imports, without modifying the source."""
        tenant_id = identifier(tenant_id)
        relative_path = _source(relative_path)
        candidate = _safe_path(self.files_root, tenant_id, "imports", relative_path)
        if not candidate.is_file() or candidate.stat().st_size > MAX_DOCUMENT_CHARS * 4:
            raise ValueError("import must be a bounded regular UTF-8 file")
        try:
            content = candidate.read_text(encoding="utf-8")
        except UnicodeError:
            raise ValueError("import must be UTF-8 text") from None
        return self.ingest(tenant_id, "mount:" + relative_path, content, **metadata)

    def search(
        self,
        tenant_id: str,
        query: str,
        *,
        as_of: str | datetime | float | None = None,
        limit: int = 5,
    ) -> list[dict]:
        tenant_id = identifier(tenant_id)
        if (
            not isinstance(query, str)
            or len(query) > 4096
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise ValueError("invalid search query or limit")
        moment = _timestamp(as_of, default=time.time())
        words = re.findall(r"[\w]+", query.lower())[:30]
        # unicode61 treats unspaced Chinese as one token. Substring scoring adds
        # CJK bigrams without pretending that ASCII word segmentation is enough.
        terms = words[:]
        for word in words:
            if re.search(r"[\u3400-\u9fff]", word):
                terms.extend(word[index : index + 2] for index in range(min(len(word) - 1, 30)))
        terms = list(dict.fromkeys(terms))[:64]
        where = "d.tenant_id=? AND d.valid_from<=? AND (d.valid_until IS NULL OR d.valid_until>?)"
        # Extract around a matching term inside SQLite. Do not materialize
        # thousands of full documents in the API worker for Chinese fallback.
        anchors = ",".join("NULLIF(instr(lower(d.content),?),0)" for _ in terms)
        anchor = f"coalesce({anchors},1)" if terms else "1"
        columns = (
            "d.id,d.tenant_id,d.source,d.kind,d.reference_path,d.created_at,"
            "d.valid_from,d.valid_until,d.sha256,length(d.content) AS content_chars,"
            f"substr(d.content,max(1,{anchor}-500),4000) AS content"
        )
        ranked: dict[str, tuple[float, sqlite3.Row]] = {}
        with self._db() as db:
            if self._fts and words:
                match = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
                rows = db.execute(
                    "SELECT " + columns + ",bm25(hub_document_fts) AS rank FROM hub_document_fts "
                    "JOIN hub_documents d ON d.id=hub_document_fts.doc_id WHERE "
                    + where
                    + " AND hub_document_fts MATCH ? ORDER BY rank LIMIT ?",
                    (*terms, tenant_id, moment, moment, match, limit),
                ).fetchall()
                ranked.update((row["id"], (10 - float(row["rank"]), row)) for row in rows)
            matches = " OR ".join("instr(lower(d.content),?)>0" for _ in terms)
            rows = db.execute(
                "SELECT "
                + columns
                + " FROM hub_documents d WHERE "
                + where
                + (" AND (" + matches + ")" if terms else "")
                + " ORDER BY created_at DESC LIMIT ?",
                (*terms, tenant_id, moment, moment, *terms, max(100, limit * 5)),
            ).fetchall()
            for row in rows:
                text = row["content"].lower()
                score = sum(min(text.count(term), 20) * len(term) for term in terms)
                if not terms or score:
                    previous = ranked.get(row["id"], (0, row))[0]
                    ranked[row["id"]] = (previous + score, row)
        results = []
        for score, row in sorted(
            ranked.values(), key=lambda item: (-item[0], -item[1]["created_at"])
        )[:limit]:
            entry = dict(row)
            entry.update({"score": score})
            results.append(entry)
        return results

    def remember(
        self,
        tenant_id: str,
        key: str,
        value: object,
        *,
        scope: str = "long",
        ttl_seconds: float | None = None,
        source: str | None = None,
    ) -> dict:
        tenant_id, scope = identifier(tenant_id), identifier(scope)
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise ValueError("invalid memory key")
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_MEMORY_BYTES:
            raise ValueError("memory exceeds the configured limit")
        if ttl_seconds is not None and (
            type(ttl_seconds) not in (int, float)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds < 0
        ):
            raise ValueError("invalid memory TTL")
        source = _source(source) if source else None
        now = time.time()
        expiry = now + ttl_seconds if ttl_seconds is not None else None
        with self._db() as db:
            db.execute(
                "INSERT INTO hub_memories VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,key) "
                "DO UPDATE SET value=excluded.value,scope=excluded.scope,source=excluded.source,"
                "updated_at=excluded.updated_at,expires_at=excluded.expires_at",
                (tenant_id, key, encoded, scope, source, now, now, expiry),
            )
        return {
            "tenant_id": tenant_id,
            "key": key,
            "value": value,
            "scope": scope,
            "source": source,
            "expires_at": expiry,
        }

    def memories(self, tenant_id: str, scope: str | None = None) -> list[dict]:
        tenant_id = identifier(tenant_id)
        params: list = [tenant_id, time.time()]
        where = "tenant_id=? AND (expires_at IS NULL OR expires_at>?)"
        if scope is not None:
            where += " AND scope=?"
            params.append(identifier(scope))
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM hub_memories WHERE " + where + " ORDER BY updated_at DESC LIMIT 200",
                params,
            ).fetchall()
        return [{**dict(row), "value": json.loads(row["value"])} for row in rows]

    def forget(self, tenant_id: str, key: str) -> bool:
        with self._db() as db:
            return (
                db.execute(
                    "DELETE FROM hub_memories WHERE tenant_id=? AND key=?",
                    (identifier(tenant_id), key),
                ).rowcount
                > 0
            )

    def assemble(
        self,
        tenant_id: str,
        query: str,
        *,
        budget_chars: int = 12000,
        as_of: str | datetime | float | None = None,
    ) -> dict:
        if type(budget_chars) is not int or not 256 <= budget_chars <= 65536:
            raise ValueError("context budget must be between 256 and 65536 characters")
        documents = self.search(tenant_id, query, as_of=as_of)
        moment = _timestamp(as_of, default=time.time())
        # The current memory view cannot reconstruct overwritten historical
        # versions. Exclude later updates rather than leaking future context.
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM hub_memories WHERE tenant_id=? AND updated_at<=? "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY updated_at DESC LIMIT 200",
                (identifier(tenant_id), moment, moment),
            ).fetchall()
        memories = [{**dict(row), "value": json.loads(row["value"])} for row in rows]
        result = {
            "query": query[:256],
            "knowledge": [],
            "memories": [],
            "references": [],
            "truncated": False,
        }

        def size() -> int:
            return len(json.dumps(result, ensure_ascii=False))

        for document in documents:
            reference = {
                "id": document["id"],
                "source": document["source"],
                "reference_path": document["reference_path"],
                "sha256": document["sha256"],
            }
            result["references"].append(reference)
            if size() > budget_chars:
                result["references"].pop()
                result["truncated"] = True
                continue
            entry = {"id": document["id"], "content": ""}
            result["knowledge"].append(entry)
            available = max(0, budget_chars - size() - 8)
            entry["content"] = document["content"][:available]
            while size() > budget_chars and entry["content"]:
                entry["content"] = entry["content"][: len(entry["content"]) // 2]
            if not entry["content"]:
                result["knowledge"].pop()
            if len(entry["content"]) < document["content_chars"]:
                result["truncated"] = True
        for memory in memories:
            entry = {
                "key": memory["key"],
                "value": memory["value"],
                "scope": memory["scope"],
                "source": memory["source"],
            }
            result["memories"].append(entry)
            if size() > budget_chars:
                result["memories"].pop()
                result["truncated"] = True
        while size() > budget_chars and result["query"]:
            result["query"] = result["query"][: len(result["query"]) // 2]
            result["truncated"] = True
        return result
