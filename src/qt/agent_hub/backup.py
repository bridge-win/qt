"""Quiesced domain backup and isolated restore verification; never copies credentials.

Stop scheduler and all workers before invoking. Individual SQLite snapshots are
transactionally consistent; only quiescing writers gives cross-ledger consistency.
The API must not receive mutations during this maintenance window.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from qt.agent_hub.config import files_root, state_root

DATABASES = (
    "hub.sqlite3",
    "context.sqlite3",
    "trading.sqlite3",
    "governance.sqlite3",
    "opportunities.sqlite3",
)


def create_backup(state: Path, files: Path, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    for name in DATABASES:
        if not (state / name).is_file():
            continue
        source_db = sqlite3.connect(f"file:{state / name}?mode=ro", uri=True)
        target_db = sqlite3.connect(destination / name)
        try:
            source_db.backup(target_db)
        finally:
            source_db.close()
            target_db.close()
    # Imports remain managed by their host mount. Only owned context/artifact
    # directories enter the backup, and symbolic links are never followed.
    for tenant in files.iterdir() if files.exists() else []:
        if tenant.is_symlink() or not tenant.is_dir():
            continue
        for owned in ("documents", "artifacts"):
            folder = tenant / owned
            if folder.is_symlink() or not folder.is_dir():
                continue
            for source in folder.rglob("*"):
                if (
                    source.is_file()
                    and not source.is_symlink()
                    and not any(
                        parent.is_symlink() for parent in source.parents if parent != files.parent
                    )
                ):
                    target = destination / "files" / source.relative_to(files)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    entries = {
        path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "created_at": time.time(),
        "files": entries,
        "requires_quiesced_writers": True,
        "credentials_included": False,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def verify_restore(backup: Path) -> dict[str, Any]:
    manifest = json.loads((backup / "manifest.json").read_text())
    with tempfile.TemporaryDirectory(prefix="qt-restore-") as directory:
        restored = Path(directory)
        for name, expected in manifest["files"].items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid_backup_path")
            source = backup / relative
            if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise ValueError("backup_integrity_failed")
            target = restored / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if target.suffix == ".sqlite3":
                db = sqlite3.connect(target)
                try:
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("restored_database_integrity_failed")
                finally:
                    db.close()
    return {
        "passed": True,
        "restored_files": len(manifest["files"]),
        "scope": "isolated file restore, hashes and SQLite integrity; writers must be quiesced",
    }


def main() -> None:
    root = state_root()
    directory = root / "backups" / f"hub-{time.time_ns()}"
    create_backup(root, files_root(), directory)
    print(json.dumps({"backup": str(directory), **verify_restore(directory)}))


if __name__ == "__main__":
    main()
