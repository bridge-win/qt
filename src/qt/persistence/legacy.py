"""Preserve legacy results as auditable archives, without inventing V2 evidence."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
import tempfile
import time
from pathlib import Path

from sqlalchemy import select

from qt.persistence import Repository
from qt.persistence.repository import checksum, insert_once
from qt.persistence.schema import migration_imports

# Explicitly exclude credentials, model configs, authorization tables and raw
# browser/chat history. The old DB remains the authoritative read-only backup.
_ALLOWED = frozenset({"experiments", "research_results", "results", "strategies", "strategy_versions",
                      "trials", "forecasts", "resolutions", "forecast_resolutions", "resolution_events",
                      "execution_intents", "orders", "fills", "ledger_entries", "ledger_transactions"})


def _value(value):
    return {"encoding": "base64", "value": base64.b64encode(value).decode()} if isinstance(value, bytes) else value


def import_sqlite_archive(repo: Repository, tenant: str, source: Path) -> dict:
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("legacy_source_must_be_a_file")
    # SQLite backup API captures committed WAL pages; hashing the .sqlite file
    # alone could miss the most recent committed transactions.
    with tempfile.TemporaryDirectory(prefix="qt-legacy-") as directory:
        snapshot = Path(directory) / "snapshot.sqlite3"
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original, sqlite3.connect(snapshot) as backup:
            original.backup(backup)
        source_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        tables = []
        with sqlite3.connect(snapshot) as database:
            if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("legacy_snapshot_integrity_failed")
            names = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in sorted(names & _ALLOWED):
                # Table identifiers come exclusively from the fixed allowlist.
                cursor = database.execute(f'SELECT * FROM "{table}"')
                columns = [column[0] for column in cursor.description]
                row_hashes = []
                for number, row in enumerate(cursor):
                    payload = {"table": table, "columns": columns, "values": [_value(item) for item in row],
                               "source_hash": source_hash, "legacy_row": number,
                               "authority": "legacy_archive_not_v2_validation"}
                    identity = checksum([source_hash, table, number])
                    saved = repo.put(tenant, "legacy_archive", payload, identity)
                    row_hashes.append(saved["sha256"])
                tables.append({"table": table, "rows": len(row_hashes), "rows_hash": checksum(row_hashes)})
        manifest = {"source_hash": source_hash, "tables": tables, "schema_version": "legacy_archive_v1",
                    "trading_state_migrated": False, "validation_evidence_migrated": False,
                    "reason": "legacy_results_remain_original_not_recomputed_or_promoted"}
        with repo.transaction() as db:
            insert_once(db, migration_imports, {"source_hash": source_hash, "tenant_id": tenant,
                                                "manifest": manifest, "created_ns": time.time_ns()})
            found = db.execute(select(migration_imports.c.manifest).where(
                migration_imports.c.source_hash == source_hash, migration_imports.c.tenant_id == tenant,
            )).scalar_one()
            if found != manifest:
                raise ValueError("legacy_import_manifest_conflict")
        return manifest
