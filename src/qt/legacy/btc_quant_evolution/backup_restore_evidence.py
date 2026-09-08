# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any


DEFAULT_STRATEGY_NAME = "BtcDonchianAtr"


def evaluate_backup_archive(
    *,
    archive_path: Path,
    strategy_name: str = DEFAULT_STRATEGY_NAME,
    require_db: bool = True,
) -> dict[str, Any]:
    contents = {
        "config": False,
        "strategies_archive": False,
        "strategy_file": False,
        "trades_db": False,
    }
    artifact = _file_artifact(path=archive_path)
    failed_reasons: list[str] = []

    if not archive_path.exists():
        failed_reasons.append("backup archive missing")
        return _result(artifact=artifact, contents=contents, failed_reasons=failed_reasons)

    try:
        with tarfile.open(archive_path, "r:gz") as outer:
            members = outer.getmembers()
            names = [member.name for member in members if member.isfile()]
            contents["config"] = any(_matches_basename(name, "config.json") for name in names)
            contents["trades_db"] = any(_matches_basename(name, "tradesv3.sqlite") for name in names)
            strategies_member = next(
                (
                    member
                    for member in members
                    if member.isfile() and _matches_basename(member.name, "strategies.tar.gz")
                ),
                None,
            )
            contents["strategies_archive"] = strategies_member is not None
            if strategies_member is not None:
                contents["strategy_file"] = _strategy_archive_has_file(
                    outer=outer,
                    strategies_member=strategies_member,
                    strategy_name=strategy_name,
                )
    except (OSError, tarfile.TarError) as exc:
        failed_reasons.append(f"backup archive is not readable: {exc}")
        return _result(artifact=artifact, contents=contents, failed_reasons=failed_reasons)

    if not contents["config"]:
        failed_reasons.append("backup archive missing config.json")
    if not contents["strategies_archive"]:
        failed_reasons.append("backup archive missing strategies.tar.gz")
    elif not contents["strategy_file"]:
        failed_reasons.append(f"strategies archive missing strategies/{strategy_name}.py")
    if require_db and not contents["trades_db"]:
        failed_reasons.append("backup archive missing tradesv3.sqlite")

    return _result(artifact=artifact, contents=contents, failed_reasons=failed_reasons)


def write_backup_restore_evidence(
    *,
    output_dir: Path,
    root_dir: Path,
    archive_path: Path,
    strategy_name: str = DEFAULT_STRATEGY_NAME,
    require_db: bool = True,
) -> Path:
    report = evaluate_backup_archive(
        archive_path=archive_path,
        strategy_name=strategy_name,
        require_db=require_db,
    )
    report["archive"]["path"] = _relative_path(path=archive_path, root_dir=root_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "backup_restore.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Write restore-readiness evidence from a Freqtrade backup archive.")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY_NAME)
    parser.add_argument("--allow-missing-db", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/live-evidence"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    report_path = write_backup_restore_evidence(
        output_dir=args.output_dir,
        root_dir=args.root_dir.resolve(),
        archive_path=args.archive,
        strategy_name=args.strategy,
        require_db=not args.allow_missing_db,
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] is True else 1


def _strategy_archive_has_file(
    *,
    outer: tarfile.TarFile,
    strategies_member: tarfile.TarInfo,
    strategy_name: str,
) -> bool:
    extracted = outer.extractfile(strategies_member)
    if extracted is None:
        return False
    strategy_path = f"strategies/{strategy_name}.py"
    try:
        with tarfile.open(fileobj=io.BytesIO(extracted.read()), mode="r:gz") as strategies:
            return any(member.isfile() and member.name == strategy_path for member in strategies.getmembers())
    except (OSError, tarfile.TarError):
        return False


def _matches_basename(path: str, basename: str) -> bool:
    return Path(path).name == basename


def _file_artifact(*, path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "path": path.as_posix(),
            "sha256": None,
            "size_bytes": None,
        }
    return {
        "exists": True,
        "path": path.as_posix(),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(*, path: Path, root_dir: Path) -> str:
    try:
        return path.resolve().relative_to(root_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _result(*, artifact: dict[str, Any], contents: dict[str, bool], failed_reasons: list[str]) -> dict[str, Any]:
    return {
        "archive": artifact,
        "contents": contents,
        "failed_reasons": failed_reasons,
        "passed": not failed_reasons,
    }


if __name__ == "__main__":
    raise SystemExit(main())

