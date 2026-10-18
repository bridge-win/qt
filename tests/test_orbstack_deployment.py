"""Offline deployment-contract checks. Never create a real local login."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def load_deploy(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_values():
    return {
        "QT_ORBSTACK_PORT": "8877", "QT_ORBSTACK_USERNAME": "fixture-only",
        "QT_ORBSTACK_PASSWORD_HASH": "$2a$14$" + "A" * 53,
        "QT_WORKBENCH_ORIGIN_CLIENT_ID": "fixture_id_" + "A" * 32,
        "QT_WORKBENCH_ORIGIN_CLIENT_SECRET": "fixture_secret_" + "B" * 32,
        "QT_BTC_MONITOR_VENUE": "binance", "QT_BTC_MONITOR_ENVIRONMENT": "production",
        "QT_TELEGRAM_BOT_TOKEN": "", "QT_TELEGRAM_CHAT_ID": "",
    }


def test_private_config_roundtrip_preserves_literal_hash(tmp_path):
    helper = load_deploy("orbstack_config")
    path = tmp_path / ".env.orbstack"
    values = fixture_values()
    path.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()))
    path.chmod(0o600)
    assert helper.read_private(path) == values
    helper.validate(values)


@pytest.mark.parametrize("key,value", [
    ("QT_ORBSTACK_PORT", "80"),
    ("QT_ORBSTACK_PORT", "8877\nextra"),
    ("QT_ORBSTACK_USERNAME", "bad user"),
    ("QT_ORBSTACK_PASSWORD_HASH", ""),
    ("QT_WORKBENCH_ORIGIN_CLIENT_SECRET", ""),
    ("QT_BTC_MONITOR_VENUE", "../../other"),
    ("QT_BTC_MONITOR_ENVIRONMENT", "live"),
    ("QT_TELEGRAM_BOT_TOKEN", "123456:" + "X" * 35),
    ("QT_TELEGRAM_CHAT_ID", "@fixturebot"),
])
def test_bad_config_fails_without_private_value_in_error(key, value):
    helper = load_deploy("orbstack_config")
    values = fixture_values() | {key: value}
    with pytest.raises(helper.ConfigError) as error:
        helper.validate(values)
    if value:
        assert value not in str(error.value)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o606])
def test_read_private_rejects_group_or_world_access(tmp_path, mode):
    helper = load_deploy("orbstack_config")
    path = tmp_path / ".env.orbstack"
    path.write_text("QT_ORBSTACK_PORT=8877\n")
    path.chmod(mode)
    with pytest.raises(helper.ConfigError, match="private"):
        helper.read_private(path)


def test_read_private_rejects_symlink_and_shell_execution(tmp_path):
    helper = load_deploy("orbstack_config")
    target = tmp_path / "target"
    target.write_text("QT_ORBSTACK_PORT=$(touch SHOULD_NOT_EXIST)\n")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(helper.ConfigError):
        helper.read_private(link)
    with pytest.raises(helper.ConfigError):
        helper.read_private(target)
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_init_noninteractive_never_generates_credentials(tmp_path, monkeypatch):
    helper = load_deploy("orbstack_config")
    monkeypatch.setattr(helper.sys.stdin, "isatty", lambda: False)
    generate = Mock()
    execute = Mock()
    monkeypatch.setattr(helper.secrets, "token_urlsafe", generate)
    monkeypatch.setattr(helper.subprocess, "run", execute)
    with pytest.raises(helper.ConfigError):
        helper.initialize(tmp_path / ".env.orbstack", "orbstack")
    generate.assert_not_called()
    execute.assert_not_called()


def test_compose_defaults_are_isolated_nonroot_and_monitor_only():
    compose = yaml.safe_load((ROOT / "docker-compose.orbstack.yml").read_text())
    assert set(compose["services"]) == {"web", "api", "research-worker", "monitor", "alerts", "reviewer"}
    for name, service in compose["services"].items():
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "platform" not in service
        assert "privileged" not in service
        assert service["restart"] == "unless-stopped"
        if name != "web":
            assert "ports" not in service
    assert compose["services"]["web"]["ports"][0].startswith("127.0.0.1:")
    assert "monitor-state:/monitor:ro" in compose["services"]["api"]["volumes"]
    assert "environment" not in compose["services"]["research-worker"]
    content = (ROOT / "docker-compose.orbstack.yml").read_text()
    assert "docker.sock" not in content
    assert "BINANCE_API_KEY" not in content
    assert "API_SECRET" not in content
    assert "CODEX_API_KEY" in compose["services"]["reviewer"]["environment"]
    for name in ("api", "monitor", "alerts", "research-worker"):
        assert "CODEX_API_KEY" not in compose["services"][name].get("environment", {})
    assert "QT_TELEGRAM_BOT_TOKEN" not in compose["services"]["monitor"]["environment"]
    assert "QT_TELEGRAM_BOT_TOKEN" in compose["services"]["alerts"]["environment"]


def test_caddy_overwrites_trust_headers_and_protects_ui_api():
    config = (ROOT / "deploy" / "orbstack.Caddyfile").read_text()
    assert "\t\troute {" in config  # Preserve security checks before nested handle routes.
    assert config.index("respond @foreign_origin") < config.index("basic_auth")
    assert config.index("basic_auth") < config.index("reverse_proxy")
    assert config.index("respond @private") < config.index("root * /srv")
    assert "header_up -Authorization" in config
    for header in ("CF-Access-Client-Id", "CF-Access-Client-Secret", "X-QT-Access-Subject"):
        assert f"header_up {header} " in config
        assert f"header_up +{header}" not in config
    assert "admin off" in config
    assert "log {" not in config


def test_worker_health_readonly_and_stale_handling(tmp_path):
    helper = load_deploy("workbench_health")
    path = tmp_path / "research.sqlite3"
    assert helper.worker_healthy(path) is False
    assert not path.exists()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE research_workers (worker_id TEXT, heartbeat_at TEXT)")
        db.execute("CREATE TABLE research_jobs (claimed_by TEXT, status TEXT, attempt_heartbeat_at TEXT)")
        now = datetime.now(timezone.utc)
        db.execute("INSERT INTO research_workers VALUES (?, ?)", ("qt-orbstack-worker", (now - timedelta(minutes=5)).isoformat()))
    assert helper.worker_healthy(path) is False
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO research_jobs VALUES (?, ?, ?)", ("qt-orbstack-worker", "running", datetime.now(timezone.utc).isoformat()))
    assert helper.worker_healthy(path) is True


def test_api_probe_requires_nonempty_credentials(monkeypatch):
    helper = load_deploy("workbench_health")
    for key in ("QT_WORKBENCH_ORIGIN_CLIENT_ID", "QT_WORKBENCH_ORIGIN_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    request = Mock()
    monkeypatch.setattr(helper, "urlopen", request)
    assert helper.api_healthy() is False
    request.assert_not_called()


def test_runtime_secrets_are_excluded_from_source_and_build_context():
    for name in (".gitignore", ".dockerignore"):
        lines = (ROOT / name).read_text().splitlines()
        assert ".env.*" in lines
    example = (ROOT / "config" / "orbstack.env.example").read_text()
    for key in ("QT_ORBSTACK_USERNAME", "QT_WORKBENCH_ORIGIN_CLIENT_SECRET", "QT_TELEGRAM_BOT_TOKEN", "QT_TELEGRAM_CHAT_ID"):
        assert f"{key}=\n" in example
    assert not os.environ.get("RUN_REAL_TELEGRAM_TESTS")
