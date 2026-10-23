"""Private database backup and isolated restore verification via Docker's CLI.

Restore ALWAYS targets a fresh random database in qt-v4-test-db, never the active
production database. Immutable Parquet objects need their own off-host backup;
this manifest explicitly does not claim to be a full platform backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from v4_config import read_private, validate


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def private_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("backup must be a private regular file")


class DatabaseTools:
    def __init__(self, root: Path, env_file: Path):
        validate(read_private(env_file))
        base = ["docker", "--context", "orbstack", "compose", "--project-directory", str(root),
                "--env-file", str(env_file)]
        self.main = [*base, "--project-name", "qt-production-v4", "-f", str(root / "deploy/v4-compose.yml")]
        self.test = [*base, "--project-name", "qt-v4-test-db", "-f", str(root / "deploy/v4-test-db.yml")]

    def command(self, executable: str, arguments: list[str], *, test: bool = False) -> list[str]:
        return [*(self.test if test else self.main), "exec", "-T", "postgres", executable, *arguments]

    def psql(self, sql: str, *, test: bool = False, database: str | None = None) -> str:
        user = "qt_v4_test" if test else "qt_v4_app"
        database = database or ("qt_v4_test" if test else "qt_v4")
        result = subprocess.run(self.command("psql", ["-XqAt", "-v", "ON_ERROR_STOP=1", "-U", user,
                                                      "-d", database, "-c", sql], test=test),
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError("scoped PostgreSQL command failed; database contents were not logged")
        return result.stdout

    def fingerprints(self, *, test: bool = False, database: str | None = None,
                     snapshot: str | None = None) -> dict:
        user = "qt_v4_test" if test else "qt_v4_app"
        database = database or ("qt_v4_test" if test else "qt_v4")
        prefix = "SET timezone='UTC'; SET datestyle='ISO,YMD'; "
        if snapshot:
            if not re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", snapshot):
                raise ValueError("invalid_exported_snapshot")
            prefix += f"BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT '{snapshot}'; "
        tables = self.psql(prefix + "SELECT schemaname||'.'||tablename FROM pg_tables "
                          "WHERE schemaname IN ('qt_v4','public') ORDER BY 1", test=test, database=database)
        result = {}
        for qualified in tables.splitlines():
            if not re.fullmatch(r"(?:qt_v4|public)\.[A-Za-z_][A-Za-z0-9_]*", qualified):
                raise ValueError("unexpected_application_table_identifier")
            schema, table = qualified.split(".")
            sql = prefix + f'COPY (SELECT row_to_json(t)::text FROM "{schema}"."{table}" t ORDER BY row_to_json(t)::text COLLATE "C") TO STDOUT'
            process = subprocess.Popen(self.command("psql", ["-XqAt", "-v", "ON_ERROR_STOP=1", "-U", user,
                                                              "-d", database, "-c", sql], test=test),
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            digest, rows = hashlib.sha256(), 0
            assert process.stdout is not None
            for line in process.stdout:
                digest.update(line)
                rows += 1
            process.stdout.close()
            if process.wait() != 0:
                raise RuntimeError("database fingerprint export failed")
            result[qualified] = {"rows": rows, "sha256": digest.hexdigest()}
        return result

    def backup(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise ValueError("backup directory must not be a symlink")
        os.chmod(directory, 0o700)
        label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        destination = directory / (label + ".dump")
        holder = subprocess.Popen(self.command("psql", ["-XqAt", "-v", "ON_ERROR_STOP=1", "-U", "qt_v4_app", "-d", "qt_v4"]),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1)
        try:
            assert holder.stdin is not None and holder.stdout is not None
            holder.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT pg_export_snapshot();\n")
            holder.stdin.flush()
            snapshot = holder.stdout.readline().strip()
            if not re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", snapshot):
                raise RuntimeError("failed to establish a consistent database snapshot")
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                completed = subprocess.run(self.command("pg_dump", ["-Fc", "--no-owner", "--no-privileges",
                                                                        "--snapshot", snapshot, "-U", "qt_v4_app", "-d", "qt_v4"]),
                                           stdout=stream, stderr=subprocess.DEVNULL, check=False)
                stream.flush()
                os.fsync(stream.fileno())
            if completed.returncode:
                raise RuntimeError("consistent PostgreSQL dump failed")
            fingerprints = self.fingerprints(snapshot=snapshot)
            manifest = {"schema_version": 1, "format": "pg_dump_custom", "postgres_major": 17,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "dump_name": destination.name, "bytes": destination.stat().st_size,
                        "sha256": file_sha256(destination), "tables": fingerprints,
                        "dataset_files_included": False, "complete_platform_backup": False,
                        "scope": "database and checkpoints only; immutable Parquet needs separate off-host backup"}
            metadata_path = destination.with_suffix(".manifest.json")
            descriptor = os.open(metadata_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(manifest, stream, sort_keys=True, allow_nan=False)
                stream.write("\n")
            return destination
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            if holder.stdin is not None:
                try:
                    holder.stdin.write("ROLLBACK;\n\\q\n")
                    holder.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                holder.stdin.close()
            try:
                holder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait()

    def restore_check(self, source: Path) -> dict:
        private_file(source)
        metadata_path = source.with_suffix(".manifest.json")
        private_file(metadata_path)
        manifest = json.loads(metadata_path.read_text())
        if (manifest.get("format") != "pg_dump_custom" or manifest.get("postgres_major") != 17
                or source.stat().st_size != manifest["bytes"] or file_sha256(source) != manifest["sha256"]):
            raise ValueError("backup identity or format validation failed")
        # Random generated identifiers only. No caller-chosen target database.
        database = "qt_restore_" + uuid.uuid4().hex
        self.psql(f'CREATE DATABASE "{database}"', test=True)
        try:
            with source.open("rb") as stream:
                result = subprocess.run(self.command("pg_restore", ["--exit-on-error", "--no-owner", "--no-privileges",
                                                                      "-U", "qt_v4_test", "-d", database], test=True),
                                        stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if result.returncode:
                raise RuntimeError("isolated PostgreSQL restore failed")
            observed = self.fingerprints(test=True, database=database)
            if observed != manifest["tables"]:
                raise ValueError("restored database row counts or hashes disagree")
            return {"passed": True, "target": "isolated_ephemeral_test_database",
                    "tables": len(observed), "rows": sum(table["rows"] for table in observed.values()),
                    "dump_sha256": manifest["sha256"], "production_modified": False,
                    "complete_platform_restore": False}
        finally:
            self.psql(f'DROP DATABASE "{database}" WITH (FORCE)', test=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["backup", "restore-check"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    tools = DatabaseTools(args.root.resolve(), args.env_file.resolve())
    if args.command == "backup":
        directory = args.root / "data/runtime/production-v4/backups"
        print(json.dumps({"backup": str(tools.backup(directory)), "private": True,
                          "complete_platform_backup": False}))
    else:
        if args.path is None:
            parser.error("restore-check requires --path to a trusted local backup")
        print(json.dumps(tools.restore_check(args.path)))


if __name__ == "__main__":
    main()
