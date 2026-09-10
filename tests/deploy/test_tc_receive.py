"""Exercise the privilege boundary and recovery behavior without a real server."""

import importlib.util
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tc_receive", Path(__file__).resolve().parents[2] / "deploy/tc_receive.py"
)
assert SPEC and SPEC.loader
receiver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receiver)
SHA = "a" * 40


def make_archive(path, extra=None, sha=SHA):
    with tarfile.open(path, "w:gz") as archive:
        files = {
            "release.json": json.dumps({"sha": sha}),
            "src/app.py": "pass",
            "scripts/run.py": "pass",
            "config/config.yaml": "{}",
            "packages/package.py": "pass",
            "pyproject.toml": "[project]",
            "README.md": "QT",
            "web/dist/index.html": "<html></html>",
        }
        for name, content in files.items():
            body = content.encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
        if extra:
            archive.addfile(extra, io.BytesIO(b""))


@pytest.mark.parametrize(
    "value", ["bash", "deploy main 1", f"deploy {SHA} 0", f"deploy {SHA} 1; id"]
)
def test_rejects_arbitrary_ssh_commands(value):
    with pytest.raises(ValueError):
        receiver.command(value)


def test_accepts_only_exact_deploy_command():
    assert receiver.command(f"deploy {SHA} 123") == (SHA, 123)


@pytest.mark.parametrize(
    "name", ["../root/x", "/root/x", "src/../../x", "src/.env", "deploy/tc.Caddyfile", "web/other"]
)
def test_rejects_paths_outside_release_scope(tmp_path, name):
    archive = tmp_path / "release.tar.gz"
    make_archive(archive, tarfile.TarInfo(name))
    with pytest.raises(ValueError):
        receiver.unpack(archive, tmp_path / "out", SHA)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_rejects_links_and_special_files(tmp_path, kind):
    entry = tarfile.TarInfo("src/unsafe")
    entry.type = kind
    entry.linkname = "/root"
    archive = tmp_path / "release.tar.gz"
    make_archive(archive, entry)
    with pytest.raises(ValueError):
        receiver.unpack(archive, tmp_path / "out", SHA)


def test_rejects_duplicate_and_wrong_revision(tmp_path):
    archive = tmp_path / "release.tar.gz"
    make_archive(archive, tarfile.TarInfo("src/app.py"))
    with pytest.raises(ValueError, match="duplicate"):
        receiver.unpack(archive, tmp_path / "duplicates", SHA)
    make_archive(archive, sha="b" * 40)
    with pytest.raises(ValueError, match="manifest"):
        receiver.unpack(archive, tmp_path / "revision", SHA)


def test_unpack_validates_limits(tmp_path, monkeypatch):
    archive = tmp_path / "release.tar.gz"
    make_archive(archive)
    receiver.unpack(archive, tmp_path / "valid", SHA)
    assert (tmp_path / "valid/web/dist/index.html").exists()
    monkeypatch.setattr(receiver, "MAX_EXPANDED", 10)
    with pytest.raises(ValueError, match="limits"):
        receiver.unpack(archive, tmp_path / "large", SHA)


def populate(path, version):
    for name in receiver.CODE_DIRS:
        (path / name).mkdir(parents=True)
        (path / name / "file").write_text(version)
    for name in receiver.CODE_FILES:
        (path / name).write_text(version)
    (path / "web/dist").mkdir(parents=True)
    (path / "web/dist/index.html").write_text(version)


def test_failed_health_restores_code_and_keeps_data(tmp_path, monkeypatch):
    app, release, backup = (tmp_path / name for name in ("app", "release", "backup"))
    populate(app, "old")
    populate(release, "new")
    backup.mkdir()
    (app / "data").mkdir()
    (app / "data/state").write_text("live state")
    monkeypatch.setattr(receiver, "APP", app)
    monkeypatch.setattr(receiver, "run", lambda *args, **kwargs: None)

    def copy(source, target):
        shutil.rmtree(target)
        shutil.copytree(source, target)

    calls = []

    def health():
        calls.append((app / "web/dist/index.html").read_text())
        if len(calls) == 1:
            raise RuntimeError("new version unhealthy")

    monkeypatch.setattr(receiver, "copy_tree", copy)
    monkeypatch.setattr(receiver, "wait_for_health", health)
    with pytest.raises(RuntimeError, match="unhealthy"):
        receiver.switch_release(release, backup)
    assert calls == ["new", "old"]
    assert (app / "src/file").read_text() == "old"
    assert (app / "data/state").read_text() == "live state"


def test_stale_run_cannot_replace_current_release(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver, "STATE", tmp_path)
    (tmp_path / "current.json").write_text(json.dumps({"run_id": 99, "sha": SHA}))
    with pytest.raises(RuntimeError, match="older"):
        receiver.deploy(tmp_path, SHA, 98)
    with pytest.raises(RuntimeError, match="mismatched"):
        receiver.deploy(tmp_path, "b" * 40, 99)
