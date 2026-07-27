#!/usr/bin/env bash
# Idempotent server-side deploy for rsynced QT checkouts.
#
# This script is intended to be run as root on the Aliyun host after
# deploy/ssh-deploy.sh mirrors the local tree to /opt/qt.

set -Eeuo pipefail

INSTALL_DIR="${QT_INSTALL_DIR:-/opt/qt}"
SERVICE_USER="${QT_USER:-qt}"
WEB_PORT="${QT_DASHBOARD_PORT:-8765}"

log() { printf '[qt-deploy] %s\n' "$*"; }
die() { printf '[qt-deploy] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root."
[[ -d "${INSTALL_DIR}" ]] || die "Install directory does not exist: ${INSTALL_DIR}"
[[ -f "${INSTALL_DIR}/pyproject.toml" ]] || die "Missing pyproject.toml in ${INSTALL_DIR}"

export DEBIAN_FRONTEND=noninteractive

log "installing system packages"
apt-get update -y
apt-get install -y --no-install-recommends \
  curl ca-certificates build-essential \
  python3 python3-venv python3-pip python3-dev \
  pkg-config libssl-dev

log "preparing service user and directories"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

mkdir -p "${INSTALL_DIR}/data/runtime" "${INSTALL_DIR}/data/backtests"

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  (cd "${INSTALL_DIR}" && cp .env.example .env)
  chmod 600 "${INSTALL_DIR}/.env"
  log "seeded ${INSTALL_DIR}/.env from .env.example"
else
  log "preserved existing ${INSTALL_DIR}/.env"
fi

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

log "building virtualenv and installing project"
runuser -u "${SERVICE_USER}" -- bash -lc "
  set -euo pipefail
  cd '${INSTALL_DIR}'
  if [[ ! -d .venv ]]; then
    python3 -m venv .venv
  fi
  ./.venv/bin/pip install --upgrade pip wheel
  ./.venv/bin/pip install -e packages/btc-backtest
  ./.venv/bin/pip install -e .
"

log "installing systemd service"
install -m 0644 "${INSTALL_DIR}/deploy/qt.service" /etc/systemd/system/qt.service
systemctl daemon-reload
systemctl enable qt.service
systemctl restart qt.service

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow "${WEB_PORT}/tcp" || true
fi

log "checking local dashboard"
for attempt in {1..30}; do
  if curl -fsS -o /dev/null -w "[qt-deploy] dashboard: %{http_code}\n" "http://localhost:${WEB_PORT}/"; then
    break
  fi
  if [[ "${attempt}" -eq 30 ]]; then
    systemctl --no-pager --full status qt.service || true
    die "dashboard did not become ready on localhost:${WEB_PORT}"
  fi
  sleep 1
done
systemctl --no-pager --full status qt.service | sed -n '1,18p'
