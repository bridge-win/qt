"""Offline configuration and isolation contracts; no browser or funded venue."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def helper(name, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "deploy"))
    return importlib.import_module(name)


def test_v4_init_keeps_credentials_private_and_preserves_existing_configuration(tmp_path, monkeypatch, capsys):
    config = helper("v4_config", monkeypatch)
    path = tmp_path / "private.env"
    config.initialize(path)
    before = path.read_bytes()
    values = config.read_private(path)
    config.validate(values)
    config.initialize(path)
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600
    assert values["QT_V4_MODEL_ID"] == values["QT_V4_DEPLOYMENT_ID"] == ""
    output = capsys.readouterr().out
    assert values["QT_V4_SERVICE_TOKEN"] not in output
    assert values["QT_V4_SIGNING_KEY"] not in output
    assert values["QT_V4_POSTGRES_PASSWORD"] not in output


def test_v4_private_settings_cannot_redirect_database_to_another_project(tmp_path, monkeypatch):
    config = helper("v4_config", monkeypatch)
    path = tmp_path / "private.env"
    config.initialize(path)
    values = config.read_private(path)
    values["QT_V4_DATABASE_URL"] = "postgresql+psycopg://other:private-value@another-project:5432/foreign"
    with pytest.raises(ValueError, match="this stack") as raised:
        config.validate(values)
    assert "private-value" not in str(raised.value)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        config.read_private(path)
    link = tmp_path / "symlink.env"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="private"):
        config.read_private(link)


def test_v4_compose_keeps_processes_and_native_candidate_separate():
    compose = yaml.safe_load((ROOT / "deploy/v4-compose.yml").read_text())
    services = compose["services"]
    roles = {"control-api", "research-worker", "experiment-worker", "data-worker", "trading-runtime", "watchdog"}
    assert roles <= services.keys()
    for role in roles:
        assert services[role]["user"] == "10001:10001"
        assert services[role]["read_only"] is True
        assert services[role]["cap_drop"] == ["ALL"]
        assert "privileged" not in services[role]
        if role != "control-api":
            assert "ports" not in services[role]
    assert services["control-api"]["ports"] == ["127.0.0.1:${QT_V4_PORT:-8890}:8890"]
    assert "ports" not in services["postgres"]
    assert services["postgres"]["image"].startswith("postgres:17.9-bookworm@sha256:")
    assert services["experiment-worker"]["build"]["target"] == "native"
    assert services["native-candidate"]["profiles"] == ["candidate"]
    assert services["native-candidate"]["network_mode"] == "none"
    assert "environment" not in services["native-candidate"]
    assert "volumes" not in services["native-candidate"]
    assert services["native-baseline-check"]["network_mode"] == "none"
    assert services["research-worker"]["environment"]["QT_V4_MODEL_PROVIDER"] == "llama_cpp_local"
    assert "QT_V4_SIGNING_KEY" in services["watchdog"]["environment"]
    for role in roles - {"control-api", "trading-runtime", "watchdog"}:
        assert "QT_V4_SIGNING_KEY" not in services[role]["environment"]
    assert "docker.sock" not in (ROOT / "deploy/v4-compose.yml").read_text()


def test_v4_test_database_does_not_reuse_production_volume_or_port():
    compose = yaml.safe_load((ROOT / "deploy/v4-test-db.yml").read_text())
    assert compose["name"] == "qt-v4-test-db"
    database = compose["services"]["postgres"]
    assert "volumes" not in database
    assert database["tmpfs"]
    assert database["ports"] == ["127.0.0.1:${QT_V4_TEST_DB_PORT:-55439}:5432"]
    assert database["environment"]["POSTGRES_DB"] == "qt_v4_test"


def test_v4_wrapper_does_not_allow_shell_values_to_redirect_inspected_database(tmp_path):
    import os
    import subprocess

    # Stub Docker, not the wrapper: assert the exact subprocess environment
    # received by Compose after the private config was validated.
    docker = tmp_path / "docker"
    docker.write_text("#!/usr/bin/env python3\nimport os\n"
                      "assert not any(k.startswith('QT_V4_') or k == 'QT_EXPERIMENT_IMAGE_DIGEST' for k in os.environ)\n")
    docker.chmod(0o755)
    env_file = tmp_path / "private.env"
    config = importlib.util.spec_from_file_location("private_v4_config", ROOT / "deploy/v4_config.py")
    module = importlib.util.module_from_spec(config)
    config.loader.exec_module(module)
    module.initialize(env_file)
    environment = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
                   "QT_V4_ENV_FILE": str(env_file),
                   "QT_V4_DATABASE_URL": "postgresql+psycopg://foreign:secret@foreign/foreign",
                   "QT_EXPERIMENT_IMAGE_DIGEST": "uninspected"}
    result = subprocess.run([str(ROOT / "deploy/orbstack.sh"), "v4-config"],
                            env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def _pins(path):
    return dict(re.findall(r"^([a-zA-Z0-9_-]+)==([^\s;\\]+)", path.read_text(), re.MULTILINE))


def test_all_native_locks_keep_the_production_dependency_authority():
    production = _pins(ROOT / "requirements-production.lock")
    baseline = _pins(ROOT / "requirements-native-baseline.lock")
    candidate = _pins(ROOT / "requirements-native-candidate.lock")
    assert baseline["nautilus-trader"] == "2.0.0rc4"
    assert candidate["nautilus-trader"] == "2.0.0rc7"
    for name, version in production.items():
        assert baseline[name] == candidate[name] == version
    assert "--require-hashes" in (ROOT / "Dockerfile.production").read_text()
    assert "--require-hashes" in (ROOT / "Dockerfile.nautilus-candidate").read_text()


def test_native_economic_parity_does_not_automatically_promote_upgrade(tmp_path, monkeypatch):
    module = helper("v4_native_compare", monkeypatch)
    import json

    paths = []
    for version in ("2.0.0rc4", "2.0.0rc7"):
        path = tmp_path / version
        path.write_text(json.dumps({"engine_version": version, "fixture_sha256": "a" * 64,
                                    "evidence": {"terminal_equity": "1000"}}))
        paths.append(path)
    report = module.compare(paths)
    assert report["economic_parity"] is True
    assert report["upgrade_accepted"] is False


def test_restore_check_rejects_corrupted_backup_before_any_docker_command(tmp_path, monkeypatch):
    module = helper("v4_backup", monkeypatch)
    config = helper("v4_config", monkeypatch)
    import json

    env_file = tmp_path / "private.env"
    config.initialize(env_file)
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"not-the-original-bytes")
    archive.chmod(0o600)
    manifest = archive.with_suffix(".manifest.json")
    manifest.write_text(json.dumps({"format": "pg_dump_custom", "postgres_major": 17,
                                    "bytes": archive.stat().st_size, "sha256": "0" * 64}))
    manifest.chmod(0o600)
    tools = module.DatabaseTools(ROOT, env_file)
    monkeypatch.setattr(tools, "psql", lambda *_args, **_kwargs: pytest.fail("must reject before database mutation"))
    with pytest.raises(ValueError, match="validation failed"):
        tools.restore_check(archive)
