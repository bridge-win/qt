# Aliyun SSH Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-command SSH deployment flow that mirrors the Follow project's Aliyun access pattern and runs QT's dashboard publicly on port `8765`.

**Architecture:** Keep the existing GitHub bootstrap for fresh Git-based installs, and add a second rsync-based path for local working-tree deploys through the `follow` SSH alias. The local script mirrors the repo to `/opt/qt`; the remote script installs packages, prepares the `qt` service account and venv, preserves server secrets/runtime data, installs `qt.service`, starts the service, and runs a localhost dashboard health check.

**Tech Stack:** Bash, rsync, OpenSSH, systemd, Python venv, pytest script-contract tests.

## Global Constraints

- Default SSH host is `follow`.
- Default remote install directory is `/opt/qt`.
- Default dashboard port is `8765`.
- Do not sync `.env`, `.git`, `.venv`, cache directories, logs, or generated runtime data.
- Keep `/opt/qt/.env` server-local and seed it from `.env.example` only when missing.
- Do not affect the existing Follow deployment at `/srv/kol` or port `3888`.

---

### Task 1: Deployment Script Contract

**Files:**
- Create: `tests/test_deploy_scripts.py`
- Create: `deploy/ssh-deploy.sh`
- Create: `deploy/deploy.sh`
- Modify: `deploy/README.md`

**Interfaces:**
- Consumes: existing `deploy/qt.service`, `scripts/run_service.py`, `.env.example`, and the `follow` SSH alias.
- Produces: executable local command `deploy/ssh-deploy.sh` and remote command `deploy/deploy.sh`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_deploy_scripts.py -q`

Expected: FAIL because `deploy/ssh-deploy.sh` and `deploy/deploy.sh` do not exist.

- [ ] **Step 3: Write minimal implementation**

Create `deploy/ssh-deploy.sh`, `deploy/deploy.sh`, and update `deploy/README.md` with the SSH flow.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_deploy_scripts.py -q`

Expected: PASS.

- [ ] **Step 5: Verify shell syntax**

Run: `bash -n deploy/ssh-deploy.sh deploy/deploy.sh deploy/aliyun_bootstrap.sh`

Expected: exit code 0.

- [ ] **Step 6: Deploy and verify**

Run: `deploy/ssh-deploy.sh`

Expected: remote `qt.service` active, `curl http://localhost:8765/` returns `200`, and public `http://<follow-host>:8765/` returns `200` after the Aliyun security rule is opened if needed.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/plans/2026-07-11-aliyun-ssh-deploy.md tests/test_deploy_scripts.py deploy/ssh-deploy.sh deploy/deploy.sh deploy/README.md
git commit -m "feat: add aliyun ssh deploy"
```
