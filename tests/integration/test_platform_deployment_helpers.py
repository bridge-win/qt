from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import IO

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_CREATOR = PROJECT_ROOT / "deploy" / "create_platform_env.py"
BACKUP_SCRIPT = PROJECT_ROOT / "deploy" / "platform-backup.sh"
ENV_TEMPLATE = PROJECT_ROOT / ".env.platform.example"


def test_environment_creator_uses_exclusive_mode_0600_without_secret_output(
    tmp_path: Path,
) -> None:
    assert ENV_CREATOR.is_file()
    output = tmp_path / ".env.platform"
    image = (
        "registry.example.invalid/qt/platform@"
        "sha256:2222222222222222222222222222222222222222222222222222222222222222"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ENV_CREATOR),
            "create",
            "--template",
            str(ENV_TEMPLATE),
            "--output",
            str(output),
            "--environment",
            "production",
            "--image-reference",
            image,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    content = output.read_text(encoding="utf-8")
    password = next(
        line.removeprefix("POSTGRES_PASSWORD=")
        for line in content.splitlines()
        if line.startswith("POSTGRES_PASSWORD=")
    )
    assert password not in result.stdout
    assert password not in result.stderr
    assert len(password) >= 64
    assert f":{password}@postgres:5432/qt_platform" in content
    assert "QT_PLATFORM_ENV=production" in content
    assert f"QT_PLATFORM_IMAGE={image}" in content
    assert "__GENERATE_ME__" not in content
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    original = output.read_bytes()
    replay = subprocess.run(
        [
            sys.executable,
            str(ENV_CREATOR),
            "create",
            "--template",
            str(ENV_TEMPLATE),
            "--output",
            str(output),
            "--environment",
            "production",
            "--image-reference",
            image,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert replay.returncode != 0
    assert output.read_bytes() == original

    previous_image = (
        "registry.example.invalid/qt/platform@"
        "sha256:3333333333333333333333333333333333333333333333333333333333333333"
    )
    update = subprocess.run(
        [
            sys.executable,
            str(ENV_CREATOR),
            "set-image",
            "--output",
            str(output),
            "--image-reference",
            previous_image,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    updated = output.read_text(encoding="utf-8")
    assert password not in update.stdout
    assert password not in update.stderr
    assert f"POSTGRES_PASSWORD={password}" in updated
    assert f"QT_PLATFORM_IMAGE={previous_image}" in updated
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_environment_creator_removes_partial_file_when_write_fails(
    tmp_path: Path,
) -> None:
    from deploy.create_platform_env import create_platform_env

    output = tmp_path / ".env.platform"

    def failing_writer(stream: IO[str], content: str) -> None:
        stream.write(content[:20])
        stream.flush()
        raise OSError("injected write failure")

    with pytest.raises(OSError, match="injected write failure"):
        create_platform_env(
            template=ENV_TEMPLATE,
            output=output,
            environment="staging",
            image_reference="qt-platform:local",
            secret_factory=lambda: "x" * 64,
            writer=failing_writer,
        )

    assert not output.exists()


def test_backup_script_publishes_validated_archive_and_checksum_atomically(
    tmp_path: Path,
) -> None:
    assert BACKUP_SCRIPT.is_file()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin / "docker")
    backup_dir = tmp_path / "external-backups"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "QT_BACKUP_DIR": str(backup_dir),
        }
    )

    subprocess.run(
        [str(BACKUP_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    archives = list(backup_dir.glob("qt-platform-*.dump"))
    assert len(archives) == 1
    archive = archives[0]
    checksum = archive.with_suffix(".dump.sha256")
    assert archive.read_text(encoding="utf-8") == "validated backup\n"
    assert checksum.is_file()
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(checksum.stat().st_mode) == 0o600
    assert not list(backup_dir.glob(".*.tmp"))
    subprocess.run(
        ["sha256sum", "--check", checksum.name],
        cwd=backup_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def test_backup_script_removes_partial_artifacts_on_failure(tmp_path: Path) -> None:
    assert BACKUP_SCRIPT.is_file()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin / "docker")
    backup_dir = tmp_path / "external-backups"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "QT_BACKUP_DIR": str(backup_dir),
            "QT_TEST_BACKUP_FAIL": "1",
        }
    )

    result = subprocess.run(
        [str(BACKUP_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert backup_dir.is_dir()
    assert list(backup_dir.iterdir()) == []


def _write_fake_docker(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
set -eu
case "$*" in
    *pg_dump*)
        if [ "${QT_TEST_BACKUP_FAIL:-0}" = 1 ]; then
            exit 23
        fi
        printf 'validated backup\\n'
        ;;
    *pg_restore*)
        cat >/dev/null
        ;;
    *)
        exit 64
        ;;
esac
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
