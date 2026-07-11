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
    assert "http://localhost:${WEB_PORT}/" in script


def test_remote_deploy_preserves_env_and_installs_systemd_service() -> None:
    script = read("deploy/deploy.sh")
    assert 'INSTALL_DIR="${QT_INSTALL_DIR:-/opt/qt}"' in script
    assert 'SERVICE_USER="${QT_USER:-qt}"' in script
    assert 'WEB_PORT="${QT_DASHBOARD_PORT:-8765}"' in script
    assert "cp .env.example .env" in script
    assert "install -m 0644" in script
    assert "/etc/systemd/system/qt.service" in script
    assert "systemctl restart qt.service" in script
    assert "curl -fsS" in script


def test_deploy_readme_documents_ssh_flow_and_public_port() -> None:
    readme = read("deploy/README.md")
    assert "deploy/ssh-deploy.sh" in readme
    assert "SSH_HOST=follow" in readme
    assert "/opt/qt" in readme
    assert "8765/tcp" in readme
