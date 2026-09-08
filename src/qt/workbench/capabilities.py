"""Evidence-first migration manifest reader.

Rows are generated from exact source paths and git commits at migration time.
They remain unverified until a QT test names the migrated entry point; no
similar-name strategy is silently recorded as an equivalent implementation.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import cast


def manifest() -> list[dict[str, object]]:
    payload = json.loads(
        files("qt.workbench").joinpath("assets/migration-manifest.json").read_text(encoding="utf-8")
    )
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("migration manifest must contain an items list")
    return [cast(dict[str, object], item) for item in items if isinstance(item, dict)]


def coverage_summary() -> dict[str, int]:
    rows = manifest()
    return {
        "total": len(rows),
        "behavior_verified": sum(row.get("migration_status") == "behavior_verified" for row in rows),
        "pending_behavior_verification": sum(
            row.get("migration_status") == "source_ported_pending_behavior_verification"
            for row in rows
        ),
        "catalog_asset_verified": sum(
            row.get("migration_status") == "catalog_asset_verified" for row in rows
        ),
        "source_ported": sum(
            row.get("migration_status")
            in {
                "source_ported_pending_behavior_verification",
                "catalog_asset_verified",
                "behavior_verified",
            }
            for row in rows
        ),
        "data_pending": sum(row.get("data_validation_status") == "not_run" for row in rows),
    }
