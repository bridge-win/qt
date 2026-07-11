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
PUBLIC_HOST="${PUBLIC_HOST:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

log() { printf '[ssh-deploy] %s\n' "$*"; }

if [[ -z "${PUBLIC_HOST}" ]]; then
  PUBLIC_HOST="$(ssh -G "${SSH_HOST}" | awk '$1 == "hostname" { print $2; exit }')"
fi

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
ssh "${SSH_HOST}" "curl -fsS --ignore-content-length -o /dev/null -w '[ssh-deploy] localhost: %{http_code}\n' 'http://localhost:${WEB_PORT}/'"

log "checking public dashboard"
curl -fsS --ignore-content-length -o /dev/null -w "[ssh-deploy] public: %{http_code}\n" "http://${PUBLIC_HOST}:${WEB_PORT}/"

log "dashboard URL: http://${PUBLIC_HOST}:${WEB_PORT}/"
