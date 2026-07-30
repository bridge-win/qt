from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_ssh_deploy_defaults_to_follow_alias_and_opt_qt() -> None:
    script = read("deploy/ssh-deploy.sh")
    assert 'SSH_HOST="${SSH_HOST:-follow}"' in script
    assert 'REMOTE_DIR="${REMOTE_DIR:-/opt/qt}"' in script
    assert 'WEB_PORT="${WEB_PORT:-8765}"' in script
    assert "rsync -az --delete" in script
    assert "--exclude='.env'" in script
    assert "--exclude='.git'" in script
    assert "--exclude='.venv'" in script
    assert "deploy/deploy.sh" in script
    assert "DASHBOARD_HOST=127.0.0.1" in script
    assert "docker inspect kol-caddy" in script


def test_remote_deploy_preserves_env_and_installs_systemd_service() -> None:
    script = read("deploy/deploy.sh")
    assert 'INSTALL_DIR="${QT_INSTALL_DIR:-/opt/qt}"' in script
    assert 'SERVICE_USER="${QT_USER:-qt}"' in script
    assert 'WEB_PORT="${QT_DASHBOARD_PORT:-8765}"' in script
    assert "cp .env.example .env" in script
    assert "install -m 0644" in script
    assert "/etc/systemd/system/qt.service" in script
    assert "/etc/systemd/system/qt-research-worker.service" in script
    assert "systemctl restart qt.service" in script
    assert "systemctl restart qt-research-worker.service" in script
    assert "curl -fsS" in script
    assert script.index("pip install -e packages/btc-backtest") < script.index(
        "pip install -e ."
    )


def test_research_worker_service_is_separate_and_hardened() -> None:
    unit = read("deploy/qt-research-worker.service")
    assert "scripts/run_research_worker.py" in unit
    assert "Restart=always" in unit
    assert "NoNewPrivileges=true" in unit
    assert "User=qt" in unit


def test_deploy_configures_private_https_and_daily_data_refresh() -> None:
    script = read("deploy/deploy.sh")
    caddy = read("deploy/qt.caddy")
    timer = read("deploy/qt-research-data-refresh.timer")
    dashboard_unit = read("deploy/qt.service")
    assert "basic_auth" in caddy
    assert "reverse_proxy 127.0.0.1:8765" in caddy
    assert "qt.followkol.live" in caddy
    assert "research-auth" in script
    assert "caddy validate" in script
    assert "    basicauth " in script
    assert "USE_DOCKER_CADDY" in script
    assert "DOCKER_CADDYFILE" in script
    assert "caddy reload --config /etc/caddy/Caddyfile" in script
    assert "--dashboard-host ${DASHBOARD_HOST}" in script
    assert "https://qt.followkol.live/api/v2/backtests/health" in script
    assert "qt-research-data-refresh.timer" in script
    assert "OnCalendar=" in timer
    assert "--dashboard-host 127.0.0.1" in dashboard_unit


def test_aliyun_bootstrap_installs_local_btc_backtest_before_qt() -> None:
    script = read("deploy/aliyun_bootstrap.sh")
    assert script.index("pip install -e packages/btc-backtest") < script.index(
        "pip install -e ."
    )


def test_deploy_readme_documents_ssh_flow_and_public_https_ports() -> None:
    readme = read("deploy/README.md")
    assert "deploy/ssh-deploy.sh" in readme
    assert "SSH_HOST=follow" in readme
    assert "/opt/qt" in readme
    assert "TCP 80" in readme
    assert "443" in readme
    assert "8765 stays bound to loopback" in readme
