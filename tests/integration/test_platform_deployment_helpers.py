from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import IO

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_CREATOR = PROJECT_ROOT / "deploy" / "create_platform_env.py"
BACKUP_SCRIPT = PROJECT_ROOT / "deploy" / "platform-backup.sh"
ENV_TEMPLATE = PROJECT_ROOT / ".env.platform.example"
VALID_PRODUCTION_IMAGE = (
    "registry.example.invalid:5443/team/qt/platform@sha256:" + "a" * 64
)


def _run_env_creator(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ENV_CREATOR), *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def _write_existing_env(path: Path, *, environment: str, image: str) -> str:
    password = "x" * 64
    content = ENV_TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "QT_PLATFORM_ENV=staging": f"QT_PLATFORM_ENV={environment}",
        "POSTGRES_PASSWORD=__GENERATE_ME__": f"POSTGRES_PASSWORD={password}",
        "QT_DATABASE_URL=postgresql+psycopg://qt_platform:__GENERATE_ME__@postgres:5432/qt_platform": (
            "QT_DATABASE_URL=postgresql+psycopg://qt_platform:"
            f"{password}@postgres:5432/qt_platform"
        ),
        "QT_PLATFORM_IMAGE=qt-platform:local": f"QT_PLATFORM_IMAGE={image}",
    }
    for source, replacement in replacements.items():
        content = content.replace(source, replacement)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return content


def test_environment_creator_uses_exclusive_mode_0600_without_secret_output(
    tmp_path: Path,
) -> None:
    assert ENV_CREATOR.is_file()
    output = tmp_path / ".env.platform"
    image = VALID_PRODUCTION_IMAGE
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


@pytest.mark.parametrize(
    ("environment", "image_reference"),
    [
        ("production", "qt-platform:local"),
        ("production", "registry.example.invalid/team/qt:latest"),
        ("production", "registry.example.invalid/team/qt@sha256:" + "a" * 63),
        ("production", "registry.example.invalid/team/qt@sha256:" + "A" * 64),
        ("production", "registry.example.invalid/team/qt:release@sha256:" + "a" * 64),
        ("staging", "qt-platform"),
        ("staging", "https://registry.example.invalid/team/qt:release"),
        ("staging", "qt-platform:local\nPOSTGRES_PASSWORD=owned"),
        ("staging", "qt-platform:local\rQT_PLATFORM_ENV=production"),
        ("staging", "qt-platform:local\t#comment"),
        ("staging", "qt-platform:local #comment"),
        ("staging", "qt-platform:local=override"),
        ("staging", "qt-platform:local\x01"),
    ],
)
def test_environment_creator_rejects_invalid_or_mutable_image_references(
    tmp_path: Path,
    environment: str,
    image_reference: str,
) -> None:
    output = tmp_path / ".env.platform"

    result = _run_env_creator(
        "create",
        "--template",
        str(ENV_TEMPLATE),
        "--output",
        str(output),
        "--environment",
        environment,
        "--image-reference",
        image_reference,
    )

    assert result.returncode != 0
    assert not output.exists()


def test_environment_creator_accepts_staging_local_tag(tmp_path: Path) -> None:
    output = tmp_path / ".env.platform"

    result = _run_env_creator(
        "create",
        "--template",
        str(ENV_TEMPLATE),
        "--output",
        str(output),
        "--environment",
        "staging",
        "--image-reference",
        "qt-platform:local",
    )

    assert result.returncode == 0
    assert "QT_PLATFORM_IMAGE=qt-platform:local" in output.read_text(encoding="utf-8")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "image_reference",
    [
        "qt-platform:local",
        "registry.example.invalid/team/qt:latest",
        "registry.example.invalid/team/qt@sha256:" + "b" * 63,
        "registry.example.invalid/team/qt@sha256:" + "B" * 64,
        "registry.example.invalid/team/qt@sha256:" + "b" * 64 + "\nQT_PLATFORM_ENV=staging",
        "registry.example.invalid/team/qt@sha256:" + "b" * 64 + " #comment",
    ],
)
def test_set_image_rejects_invalid_production_reference_without_modification(
    tmp_path: Path,
    image_reference: str,
) -> None:
    output = tmp_path / ".env.platform"
    _write_existing_env(
        output,
        environment="production",
        image=VALID_PRODUCTION_IMAGE,
    )
    original = output.read_bytes()
    original_mode = stat.S_IMODE(output.stat().st_mode)

    result = _run_env_creator(
        "set-image",
        "--output",
        str(output),
        "--image-reference",
        image_reference,
    )

    assert result.returncode != 0
    assert output.read_bytes() == original
    assert stat.S_IMODE(output.stat().st_mode) == original_mode == 0o600


@pytest.mark.parametrize(
    "case",
    [
        "duplicate-environment",
        "duplicate-image",
        "duplicate-password",
        "malformed-assignment",
        "malicious-image",
        "mutable-production-image",
    ],
)
def test_set_image_rejects_malicious_or_duplicate_existing_environment(
    tmp_path: Path,
    case: str,
) -> None:
    output = tmp_path / ".env.platform"
    content = _write_existing_env(
        output,
        environment="production",
        image=VALID_PRODUCTION_IMAGE,
    )
    if case == "duplicate-environment":
        content += "QT_PLATFORM_ENV=production\n"
    elif case == "duplicate-image":
        content += f"QT_PLATFORM_IMAGE={VALID_PRODUCTION_IMAGE}\n"
    elif case == "duplicate-password":
        content += f"POSTGRES_PASSWORD={'x' * 64}\n"
    elif case == "malformed-assignment":
        content += f"export QT_PLATFORM_IMAGE={VALID_PRODUCTION_IMAGE}\n"
    elif case == "malicious-image":
        content = content.replace(
            f"QT_PLATFORM_IMAGE={VALID_PRODUCTION_IMAGE}",
            "QT_PLATFORM_IMAGE=qt-platform:local # injected",
        )
    else:
        content = content.replace(
            f"QT_PLATFORM_IMAGE={VALID_PRODUCTION_IMAGE}",
            "QT_PLATFORM_IMAGE=qt-platform:local",
        )
    output.write_text(content, encoding="utf-8")
    output.chmod(0o600)
    original = output.read_bytes()

    result = _run_env_creator(
        "set-image",
        "--output",
        str(output),
        "--image-reference",
        VALID_PRODUCTION_IMAGE.replace("a" * 64, "b" * 64),
    )

    assert result.returncode != 0
    assert output.read_bytes() == original
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_environment_creator_rejects_duplicate_template_assignments(
    tmp_path: Path,
) -> None:
    template = tmp_path / ".env.platform.example"
    template.write_text(
        ENV_TEMPLATE.read_text(encoding="utf-8")
        + "QT_PLATFORM_IMAGE=qt-platform:local\n",
        encoding="utf-8",
    )
    output = tmp_path / ".env.platform"

    result = _run_env_creator(
        "create",
        "--template",
        str(template),
        "--output",
        str(output),
        "--environment",
        "staging",
        "--image-reference",
        "qt-platform:local",
    )

    assert result.returncode != 0
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
    assert not list(backup_dir.glob(".*.tmp*"))
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


def test_concurrent_same_second_failure_cannot_remove_successful_backup(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin / "docker")
    _write_fake_date(fake_bin / "date")
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    backup_dir = tmp_path / "external-backups"
    base_environment = os.environ.copy()
    base_environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{base_environment['PATH']}",
            "QT_BACKUP_DIR": str(backup_dir),
            "QT_TEST_BARRIER_DIR": str(barrier_dir),
        }
    )
    success_environment = base_environment.copy()
    failure_environment = base_environment.copy()
    failure_environment["QT_TEST_BACKUP_FAIL_AFTER_PUBLICATION"] = "1"

    processes = [
        subprocess.Popen(
            [str(BACKUP_SCRIPT)],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        for environment in (success_environment, failure_environment)
    ]
    results: list[tuple[int, str, str]] = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            results.append((process.returncode, stdout, stderr))
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    assert sum(returncode == 0 for returncode, _stdout, _stderr in results) == 1
    assert sum(returncode != 0 for returncode, _stdout, _stderr in results) == 1
    archives = list(backup_dir.glob("qt-platform-20260721T010203Z-*.dump"))
    assert len(archives) == 1
    archive = archives[0]
    checksum = archive.with_suffix(".dump.sha256")
    assert archive.read_text(encoding="utf-8") == "validated backup\n"
    assert checksum.is_file()
    assert not list(backup_dir.glob(".*.tmp*"))
    subprocess.run(
        ["sha256sum", "--check", checksum.name],
        cwd=backup_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def test_backup_signal_cleans_owned_temporary_files_and_next_run_succeeds(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin / "docker")
    _write_fake_date(fake_bin / "date")
    _write_fake_ln(fake_bin / "ln")
    backup_dir = tmp_path / "external-backups"
    published_marker = tmp_path / "published"
    release_marker = tmp_path / "release"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "QT_BACKUP_DIR": str(backup_dir),
            "QT_TEST_PUBLISHED_MARKER": str(published_marker),
            "QT_TEST_RELEASE_FILE": str(release_marker),
        }
    )
    process = subprocess.Popen(
        [str(BACKUP_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not published_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert published_marker.exists()

        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert process.returncode != 0
    assert backup_dir.is_dir()
    assert list(backup_dir.iterdir()) == []

    clean_environment = environment.copy()
    clean_environment.pop("QT_TEST_PUBLISHED_MARKER")
    clean_environment.pop("QT_TEST_RELEASE_FILE")
    subprocess.run(
        [str(BACKUP_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=clean_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    archives = list(backup_dir.glob("qt-platform-20260721T010203Z-*.dump"))
    assert len(archives) == 1
    checksum = archives[0].with_suffix(".dump.sha256")
    subprocess.run(
        ["sha256sum", "--check", checksum.name],
        cwd=backup_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _write_fake_docker(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
set -eu
case "$*" in
    *pg_dump*)
        if [ -n "${QT_TEST_BARRIER_DIR:-}" ]; then
            : > "$QT_TEST_BARRIER_DIR/$$.ready"
            while [ "$(find "$QT_TEST_BARRIER_DIR" -type f -name '*.ready' | wc -l | tr -d ' ')" -lt 2 ]; do
                sleep 0.05
            done
        fi
        if [ "${QT_TEST_BACKUP_FAIL_AFTER_PUBLICATION:-0}" = 1 ]; then
            while ! find "$QT_BACKUP_DIR" -type f -name 'qt-platform-*.dump' | grep -q .; do
                sleep 0.05
            done
            exit 24
        fi
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


def _write_fake_date(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\nprintf '20260721T010203Z\\n'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_fake_ln(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
set -eu
/bin/ln "$@"
case "${2:-}" in
    *qt-platform-*.dump)
        if [ -n "${QT_TEST_PUBLISHED_MARKER:-}" ]; then
            : > "$QT_TEST_PUBLISHED_MARKER"
            while [ ! -e "$QT_TEST_RELEASE_FILE" ]; do
                sleep 0.05
            done
        fi
        ;;
esac
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
