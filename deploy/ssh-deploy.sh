#!/usr/bin/env bash
# Deploy this local checkout to the Alibaba Cloud host used by the Follow project.
#
# Prereq: ~/.ssh/config has a host alias named `follow`.
# Usage:
#   deploy/ssh-deploy.sh
#
# Overrides:
#   SSH_HOST=follow REMOTE_DIR=/opt/qt WEB_PORT=8765 deploy/ssh-deploy.sh

set -Eeuo pipefail

SSH_HOST="${SSH_HOST:-follow}"
REMOTE_DIR="${REMOTE_DIR:-/opt/qt}"
WEB_PORT="${WEB_PORT:-8765}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

log() { printf '[ssh-deploy] %s\n' "$*"; }

log "syncing ${ROOT}/ to ${SSH_HOST}:${REMOTE_DIR}/"
ssh "${SSH_HOST}" "mkdir -p '${REMOTE_DIR}'"
rsync -az --delete \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.mypy_cache' \
  --exclude='.ruff_cache' \
  --exclude='.DS_Store' \
  --exclude='*.log' \
  --exclude='.env' \
  --exclude='data/runtime' \
  --exclude='data/backtests' \
  --exclude='data/parquet' \
  --exclude='data/reports' \
  ./ "${SSH_HOST}:${REMOTE_DIR}/"

log "running remote deploy"
ssh "${SSH_HOST}" "cd '${REMOTE_DIR}' && chmod +x deploy/deploy.sh && QT_INSTALL_DIR='${REMOTE_DIR}' QT_DASHBOARD_PORT='${WEB_PORT}' deploy/deploy.sh"

log "checking remote localhost dashboard"
ssh "${SSH_HOST}" "
  DASHBOARD_HOST=127.0.0.1
  if ! curl -fsS --ignore-content-length -o /dev/null \
      'http://\${DASHBOARD_HOST}:${WEB_PORT}/'; then
    DASHBOARD_HOST=\$(
      docker inspect kol-caddy \
        --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
    )
  fi
  curl -fsS --ignore-content-length -o /dev/null \
    -w '[ssh-deploy] dashboard: %{http_code}\\n' \
    \"http://\${DASHBOARD_HOST}:${WEB_PORT}/\"
"

log "checking public HTTPS authentication boundary"
PUBLIC_STATUS="$(
  curl -sS --ignore-content-length -o /dev/null -w '%{http_code}' \
    "https://qt.followkol.live/"
)"
[[ "${PUBLIC_STATUS}" == "401" ]] || {
  printf '[ssh-deploy] expected public HTTP 401, got %s\n' "${PUBLIC_STATUS}" >&2
  exit 1
}
printf '[ssh-deploy] public authentication: %s\n' "${PUBLIC_STATUS}"

log "dashboard URL: https://qt.followkol.live/backtest/build"
