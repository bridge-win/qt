"""Versioned, tenant-scoped evidence and memory without a model service.

``as_of`` is the business time at which evidence must be valid *and publicly
available*. ``known_at`` additionally limits what this installation had observed.
Historical backfills therefore need an explicit availability timestamp and its
provenance; setting a historical valid_from alone never makes evidence historical.
"""

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
MAX_CHUNK_CHARS = 8000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


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
            db.execute("PRAGMA journal_mode=WAL")
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
            self._migrate(db)
            try:
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS hub_document_fts USING fts5("
                    "doc_id UNINDEXED, tenant_id UNINDEXED, content, tokenize='unicode61')"
                )
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False
        self.path.chmod(0o600)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Serialize additive migration across workers; legacy availability is conservative."""
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] > 2:
            raise ValueError("context database schema is newer than this runtime")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(hub_documents)")}
        for name, declaration in {
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "available_at": "REAL",
            "observed_at": "REAL",
            "published_at": "REAL",
            "provenance": "TEXT",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE hub_documents ADD COLUMN {name} {declaration}")
        db.execute(
            "UPDATE hub_documents SET available_at=coalesce(available_at,created_at),"
            "observed_at=coalesce(observed_at,created_at)"
        )
        if "revision" not in columns:
            # Old sources could be imported repeatedly. Preserve each version in
            # chronological order without pretending the old content was known earlier.
            rows = db.execute(
                "SELECT id,tenant_id,source,kind FROM hub_documents "
                "ORDER BY tenant_id,source,kind,created_at,id"
            ).fetchall()
            counts: dict[tuple[str, str, str], int] = {}
            for row in rows:
                key = (row["tenant_id"], row["source"], row["kind"])
                counts[key] = counts.get(key, 0) + 1
                db.execute(
                    "UPDATE hub_documents SET revision=? WHERE id=?", (counts[key], row["id"])
                )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS hub_documents_revision "
            "ON hub_documents(tenant_id,source,kind,revision)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS hub_memory_versions ("
            "tenant_id TEXT NOT NULL,key TEXT NOT NULL,revision INTEGER NOT NULL,"
            "value TEXT NOT NULL,scope TEXT NOT NULL,source TEXT,"
            "created_at REAL NOT NULL,updated_at REAL NOT NULL,expires_at REAL,"
            "sha256 TEXT NOT NULL,PRIMARY KEY(tenant_id,key,revision))"
        )
        # Keep the old table as the current view for compatibility. Only the
        # surviving legacy version can be recovered; do not invent lost history.
        for row in db.execute(
            "SELECT m.* FROM hub_memories m WHERE NOT EXISTS (SELECT 1 FROM "
            "hub_memory_versions v WHERE v.tenant_id=m.tenant_id AND v.key=m.key)"
        ).fetchall():
            db.execute(
                "INSERT INTO hub_memory_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row["tenant_id"],
                    row["key"],
                    1,
                    row["value"],
                    row["scope"],
                    row["source"],
                    row["created_at"],
                    row["updated_at"],
                    row["expires_at"],
                    hashlib.sha256(row["value"].encode()).hexdigest(),
                ),
            )
        db.execute("PRAGMA user_version=2")
        db.commit()

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
        available_at: str | datetime | float | None = None,
        published_at: str | datetime | float | None = None,
        provenance: str | None = None,
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
                available_at=available_at,
                published_at=published_at,
                provenance=provenance,
            )
        if not isinstance(content, str) or not 1 <= len(content) <= MAX_DOCUMENT_CHARS:
            raise ValueError("document is empty or exceeds the configured limit")
        now = time.time()
        start, end = _timestamp(valid_from, default=now), _timestamp(valid_until)
        available, published = _timestamp(available_at, default=now), _timestamp(published_at)
        provenance = _source(provenance) if provenance is not None else None
        if end is not None and end <= start:
            raise ValueError("valid_until must follow valid_from")
        if available < now and provenance is None:
            raise ValueError("historical availability requires provenance")
        if published is not None and published > available:
            raise ValueError("published_at cannot follow available_at")
        document_id = uuid.uuid4().hex
        reference = _safe_path(self.files_root, tenant_id, "documents", document_id + ".txt")
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                "SELECT *,length(content) AS content_chars FROM hub_documents "
                "WHERE tenant_id=? AND source=? AND kind=? ORDER BY revision DESC LIMIT 1",
                (tenant_id, source, kind),
            ).fetchone()
            if (
                latest is not None
                and latest["sha256"] == digest
                and (valid_from is None or latest["valid_from"] == start)
                and latest["valid_until"] == end
                and (available_at is None or latest["available_at"] == available)
                and latest["published_at"] == published
                and latest["provenance"] == provenance
            ):
                result = dict(latest)
                result.pop("content")
                return result
            revision = latest["revision"] + 1 if latest is not None else 1
            reference.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Unique ID + exclusive creation prevents replacing an existing artifact.
            with reference.open("x", encoding="utf-8") as handle:
                handle.write(content)
            reference.chmod(0o600)
            db.execute(
                "INSERT INTO hub_documents (id,tenant_id,source,kind,content,reference_path,"
                "created_at,valid_from,valid_until,sha256,revision,available_at,observed_at,"
                "published_at,provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    revision,
                    available,
                    now,
                    published,
                    provenance,
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
            "revision": revision,
            "available_at": available,
            "observed_at": now,
            "published_at": published,
            "provenance": provenance,
            "content_chars": len(content),
        }

    @staticmethod
    def _visible(alias: str, tenant: str, moment: float, known: float | None) -> tuple[str, list]:
        """Business validity plus availability, with optional observation cutoff.

        A later eligible revision supersedes an earlier version even after the
        new version expires. Expiry must not resurrect withdrawn old guidance.
        """
        where = (
            f"{alias}.tenant_id=? AND {alias}.valid_from<=? "
            f"AND ({alias}.valid_until IS NULL OR {alias}.valid_until>?) "
            f"AND {alias}.available_at<=?"
        )
        params = [tenant, moment, moment, moment]
        if known is not None:
            where += f" AND {alias}.observed_at<=?"
            params.append(known)
        where += (
            " AND NOT EXISTS (SELECT 1 FROM hub_documents newer WHERE "
            f"newer.tenant_id={alias}.tenant_id AND newer.source={alias}.source "
            f"AND newer.kind={alias}.kind AND newer.revision>{alias}.revision "
            "AND newer.valid_from<=? AND newer.available_at<=?"
        )
        params.extend([moment, moment])
        if known is not None:
            where += " AND newer.observed_at<=?"
            params.append(known)
        return where + ")", params

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
        known_at: str | datetime | float | None = None,
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
        known = _timestamp(known_at)
        words = re.findall(r"[\w]+", query.lower())[:30]
        # unicode61 treats unspaced Chinese as one token. Substring scoring adds
        # CJK bigrams without pretending that ASCII word segmentation is enough.
        terms = words[:]
        for word in words:
            if re.search(r"[\u3400-\u9fff]", word):
                terms.extend(word[index : index + 2] for index in range(min(len(word) - 1, 30)))
        terms = list(dict.fromkeys(terms))[:64]
        where, params = self._visible("d", tenant_id, moment, known)
        # Extract around a matching term inside SQLite. Do not materialize
        # thousands of full documents in the API worker for Chinese fallback.
        anchors = ",".join("NULLIF(instr(lower(d.content),?),0)" for _ in terms)
        anchor = f"coalesce({anchors},1)" if terms else "1"
        columns = (
            "d.id,d.tenant_id,d.source,d.kind,d.reference_path,d.created_at,"
            "d.valid_from,d.valid_until,d.sha256,length(d.content) AS content_chars,"
            "d.revision,d.available_at,d.observed_at,d.published_at,d.provenance,"
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
                    (*terms, *params, match, limit),
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
                (*terms, *params, *terms, max(100, limit * 5)),
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

    @staticmethod
    def _reference_map(references: list[dict]) -> dict[str, str]:
        if not isinstance(references, list) or len(references) > 200:
            raise ValueError("invalid run evidence allowlist")
        result = {}
        for reference in references:
            if not isinstance(reference, dict):
                raise ValueError("invalid run evidence allowlist")
            key, digest = reference.get("id"), reference.get("sha256")
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 256
                or not isinstance(digest, str)
                or not _DIGEST.fullmatch(digest)
                or (key in result and result[key] != digest)
            ):
                raise ValueError("invalid run evidence allowlist")
            result[key] = digest
        return result

    def _verified_content(self, row: sqlite3.Row) -> str:
        content = row["content"]
        expected = row["sha256"]
        if hashlib.sha256(content.encode()).hexdigest() != expected:
            raise ValueError("evidence_integrity_mismatch")
        path = _safe_path(self.files_root, row["tenant_id"], "documents", row["id"] + ".txt")
        if str(path) != row["reference_path"] or not path.is_file():
            raise ValueError("evidence_artifact_unavailable")
        with path.open("rb") as handle:
            original = handle.read(MAX_DOCUMENT_CHARS * 4 + 1)
        if hashlib.sha256(original).hexdigest() != expected:
            raise ValueError("evidence_integrity_mismatch")
        return content

    def validate_evidence(
        self,
        tenant_id: str,
        references: list[str | dict],
        allowed_references: list[dict],
        *,
        as_of: str | datetime | float | None = None,
        known_at: str | datetime | float | None = None,
    ) -> dict:
        """Resolve model citations against a *server-owned* run allowlist.

        This validates membership, version/hash, tenant, time and stored bytes.
        It cannot prove that the source supports a model's claim. The caller must
        never build ``allowed_references`` from a model response or request body.
        """
        tenant_id = identifier(tenant_id)
        if not isinstance(references, list) or not 1 <= len(references) <= 50:
            raise ValueError("evidence must contain between 1 and 50 references")
        allowed = self._reference_map(allowed_references)
        moment, known = _timestamp(as_of, default=time.time()), _timestamp(known_at)
        where, params = self._visible("d", tenant_id, moment, known)
        verified, errors = [], []
        with self._db() as db:
            for reference in references:
                key = (
                    reference
                    if isinstance(reference, str)
                    else (reference.get("id") if isinstance(reference, dict) else None)
                )
                if not isinstance(key, str) or key not in allowed:
                    errors.append(
                        {
                            "id": key if isinstance(key, str) else None,
                            "reason": "evidence_not_in_run",
                        }
                    )
                    continue
                digest = (
                    reference.get("sha256", allowed[key])
                    if isinstance(reference, dict)
                    else allowed[key]
                )
                if digest != allowed[key]:
                    errors.append({"id": key, "reason": "evidence_hash_mismatch"})
                    continue
                row = db.execute(
                    "SELECT d.* FROM hub_documents d WHERE " + where + " AND d.id=?",
                    (*params, key),
                ).fetchone()
                if row is None:
                    errors.append({"id": key, "reason": "evidence_unavailable"})
                    continue
                if row["sha256"] != digest:
                    errors.append({"id": key, "reason": "evidence_hash_mismatch"})
                    continue
                try:
                    self._verified_content(row)
                except (OSError, ValueError):
                    errors.append({"id": key, "reason": "evidence_integrity_mismatch"})
                    continue
                verified.append(
                    {
                        "id": key,
                        "sha256": digest,
                        "source": row["source"],
                        "revision": row["revision"],
                        "available_at": row["available_at"],
                        "observed_at": row["observed_at"],
                        "valid_from": row["valid_from"],
                        "valid_until": row["valid_until"],
                    }
                )
        return {"valid": not errors, "references": verified, "errors": errors}

    def read_chunk(
        self,
        tenant_id: str,
        document_id: str,
        expected_sha256: str,
        allowed_references: list[dict],
        *,
        offset: int = 0,
        limit_chars: int = 4000,
        as_of: str | datetime | float | None = None,
        known_at: str | datetime | float | None = None,
    ) -> dict:
        """Read bounded Unicode characters from an authorized immutable version."""
        if (
            type(offset) is not int
            or offset < 0
            or offset > MAX_DOCUMENT_CHARS
            or type(limit_chars) is not int
            or not 1 <= limit_chars <= MAX_CHUNK_CHARS
        ):
            raise ValueError("invalid evidence chunk bounds")
        validation = self.validate_evidence(
            tenant_id,
            [{"id": document_id, "sha256": expected_sha256}],
            allowed_references,
            as_of=as_of,
            known_at=known_at,
        )
        if not validation["valid"]:
            raise ValueError(validation["errors"][0]["reason"])
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM hub_documents WHERE tenant_id=? AND id=?",
                (identifier(tenant_id), document_id),
            ).fetchone()
            if row is None:
                raise ValueError("evidence_unavailable")
            content = self._verified_content(row)
        if offset > len(content):
            raise ValueError("evidence chunk offset exceeds document")
        chunk = content[offset : offset + limit_chars]
        end = offset + len(chunk)
        return {
            **validation["references"][0],
            "offset": offset,
            "content": chunk,
            "content_chars": len(content),
            "next_offset": end if end < len(content) else None,
            "chunk_sha256": hashlib.sha256(chunk.encode()).hexdigest(),
            "trust": "untrusted_source",
        }

    def retrieve_chunks(
        self,
        tenant_id: str,
        query: str,
        allowed_references: list[dict],
        *,
        limit: int = 5,
        limit_chars: int = 4000,
        as_of: str | datetime | float | None = None,
        known_at: str | datetime | float | None = None,
    ) -> list[dict]:
        """Recover matching passages from the run corpus, including truncated tails.

        Retrieval is deterministic lexical matching; no semantic-model or
        arbitrary filesystem access is implied.
        """
        if not isinstance(query, str) or not 1 <= len(query) <= 4096:
            raise ValueError("invalid chunk query")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("invalid chunk limit")
        if type(limit_chars) is not int or not 1 <= limit_chars <= MAX_CHUNK_CHARS:
            raise ValueError("invalid evidence chunk bounds")
        allowed = self._reference_map(allowed_references)
        if len(allowed) > 50:
            raise ValueError("chunk retrieval supports at most 50 run references")
        if not allowed:
            return []
        validation = self.validate_evidence(
            tenant_id, list(allowed), allowed_references, as_of=as_of, known_at=known_at
        )
        terms = re.findall(r"[\w]+", query.lower())[:30]
        ranked = []
        with self._db() as db:
            for reference in validation["references"]:
                row = db.execute(
                    "SELECT content FROM hub_documents WHERE tenant_id=? AND id=?",
                    (identifier(tenant_id), reference["id"]),
                ).fetchone()
                if row is None:
                    continue
                content = row["content"].lower()
                hits = [content.find(term) for term in terms if term in content]
                if hits:
                    ranked.append((len(hits), max(0, min(hits) - limit_chars // 4), reference))
        ranked.sort(key=lambda item: (-item[0], item[2]["id"]))
        return [
            self.read_chunk(
                tenant_id,
                reference["id"],
                reference["sha256"],
                allowed_references,
                offset=offset,
                limit_chars=limit_chars,
                as_of=as_of,
                known_at=known_at,
            )
            for _, offset, reference in ranked[:limit]
        ]

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
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT revision,created_at,updated_at FROM hub_memory_versions "
                "WHERE tenant_id=? AND key=? ORDER BY revision DESC LIMIT 1",
                (tenant_id, key),
            ).fetchone()
            if previous is not None and previous["updated_at"] > now:
                raise ValueError("memory clock moved backwards")
            revision = previous["revision"] + 1 if previous is not None else 1
            created_at = previous["created_at"] if previous is not None else now
            db.execute(
                "INSERT INTO hub_memory_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, key, revision, encoded, scope, source, created_at, now, expiry, digest),
            )
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
            "revision": revision,
            "sha256": digest,
            "created_at": created_at,
            "updated_at": now,
        }

    def memories(
        self,
        tenant_id: str,
        scope: str | None = None,
        *,
        as_of: str | datetime | float | None = None,
        known_at: str | datetime | float | None = None,
    ) -> list[dict]:
        """Return the last recorded version at the cutoff, then apply TTL/scope.

        Memory is an observation by this system, so it cannot be backdated. A
        replacement's expiry never resurrects its previous version.
        """
        tenant_id = identifier(tenant_id)
        moment = _timestamp(as_of, default=time.time())
        known = _timestamp(known_at, default=moment)
        cutoff = min(moment, known)
        params: list = [tenant_id, cutoff, moment, cutoff]
        where = (
            "v.tenant_id=? AND v.updated_at<=? "
            "AND (v.expires_at IS NULL OR v.expires_at>?) AND NOT EXISTS "
            "(SELECT 1 FROM hub_memory_versions newer WHERE newer.tenant_id=v.tenant_id "
            "AND newer.key=v.key AND newer.revision>v.revision AND newer.updated_at<=?)"
        )
        if scope is not None:
            where += " AND v.scope=?"
            params.append(identifier(scope))
        with self._db() as db:
            rows = db.execute(
                "SELECT v.* FROM hub_memory_versions v WHERE "
                + where
                + " ORDER BY v.updated_at DESC,v.revision DESC LIMIT 200",
                params,
            ).fetchall()
        result = []
        for row in rows:
            if hashlib.sha256(row["value"].encode()).hexdigest() != row["sha256"]:
                raise ValueError("memory_integrity_mismatch")
            result.append({**dict(row), "value": json.loads(row["value"])})
        return result

    def forget(self, tenant_id: str, key: str) -> bool:
        """Logically erase every version; there is no in-app recoverable tombstone.

        Database pages, WAL files and external backups follow their own retention
        policy. This operation does not claim secure physical erasure.
        """
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            params = (identifier(tenant_id), key)
            removed = db.execute(
                "DELETE FROM hub_memory_versions WHERE tenant_id=? AND key=?", params
            ).rowcount
            current = db.execute(
                "DELETE FROM hub_memories WHERE tenant_id=? AND key=?", params
            ).rowcount
            return removed + current > 0

    def assemble(
        self,
        tenant_id: str,
        query: str,
        *,
        budget_chars: int = 12000,
        as_of: str | datetime | float | None = None,
        known_at: str | datetime | float | None = None,
    ) -> dict:
        if type(budget_chars) is not int or not 256 <= budget_chars <= 65536:
            raise ValueError("context budget must be between 256 and 65536 characters")
        documents = self.search(tenant_id, query, as_of=as_of, known_at=known_at)
        memories = self.memories(tenant_id, as_of=as_of, known_at=known_at)
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
                "revision": document["revision"],
                "available_at": document["available_at"],
                "observed_at": document["observed_at"],
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
                "revision": memory["revision"],
                "sha256": memory["sha256"],
                "trust": "unverified_memory",
            }
            result["memories"].append(entry)
            if size() > budget_chars:
                result["memories"].pop()
                result["truncated"] = True
        while size() > budget_chars and result["query"]:
            result["query"] = result["query"][: len(result["query"]) // 2]
            result["truncated"] = True
        return result
