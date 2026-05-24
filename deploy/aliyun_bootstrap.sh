#!/usr/bin/env bash
# QT — one-line bootstrap for Aliyun Lighthouse / ECS (Ubuntu 22.04+ / Debian 12).
#
# Usage on a fresh Lighthouse box (as root or with sudo):
#
#   curl -fsSL https://raw.githubusercontent.com/bridge-win/qt/main/deploy/aliyun_bootstrap.sh | sudo bash
#
# Optional environment overrides:
#   QT_REPO_URL   git repo to clone     (default: https://github.com/bridge-win/qt.git)
#   QT_REPO_REF   branch / tag / sha    (default: main)
#   QT_INSTALL_DIR install directory    (default: /opt/qt)
#   QT_USER       service user          (default: qt)
#
# What it does:
#   1. apt-installs python3.11+, git, build deps
#   2. creates the `qt` system user and /opt/qt
#   3. clones (or pulls) the repo
#   4. builds a venv and installs the project
#   5. seeds .env from .env.example (only if missing)
#   6. installs and starts the systemd unit `qt.service`
#
# After it finishes, fill in /opt/qt/.env (SMTP + Telegram + exchange keys)
# then `systemctl restart qt`.

set -Eeuo pipefail

REPO_URL="${QT_REPO_URL:-https://github.com/bridge-win/qt.git}"
REPO_REF="${QT_REPO_REF:-main}"
INSTALL_DIR="${QT_INSTALL_DIR:-/opt/qt}"
SERVICE_USER="${QT_USER:-qt}"

log()  { printf '\033[1;32m[qt-bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[qt-bootstrap]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[qt-bootstrap]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (use sudo)."

log "1/6 installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  git curl ca-certificates build-essential \
  python3 python3-venv python3-pip python3-dev \
  pkg-config libssl-dev

log "2/6 creating service user '${SERVICE_USER}' and ${INSTALL_DIR}"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
mkdir -p "${INSTALL_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

log "3/6 fetching repo ${REPO_URL}@${REPO_REF}"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  sudo -u "${SERVICE_USER}" git -C "${INSTALL_DIR}" fetch --all --prune
  sudo -u "${SERVICE_USER}" git -C "${INSTALL_DIR}" checkout "${REPO_REF}"
  sudo -u "${SERVICE_USER}" git -C "${INSTALL_DIR}" pull --ff-only origin "${REPO_REF}" || true
else
  sudo -u "${SERVICE_USER}" git clone --branch "${REPO_REF}" --depth 50 "${REPO_URL}" "${INSTALL_DIR}"
fi

log "4/6 building venv and installing project"
sudo -u "${SERVICE_USER}" bash -lc "
  set -e
  cd '${INSTALL_DIR}'
  if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
  ./.venv/bin/pip install --upgrade pip wheel
  ./.venv/bin/pip install -e .
  mkdir -p data/runtime data/backtests
"

log "5/6 seeding .env (only if missing)"
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  sudo -u "${SERVICE_USER}" cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  chmod 600 "${INSTALL_DIR}/.env"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/.env"
  warn "Created ${INSTALL_DIR}/.env from template — edit it to add SMTP + Telegram + exchange keys."
else
  log ".env already present — left untouched"
fi

log "6/6 installing systemd unit qt.service"
install -m 0644 "${INSTALL_DIR}/deploy/qt.service" /etc/systemd/system/qt.service
systemctl daemon-reload
systemctl enable qt.service
systemctl restart qt.service

# Open dashboard port through ufw if present (Lighthouse security group must also allow it).
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 8765/tcp || true
fi

cat <<EOF

\033[1;32m✓ QT installed.\033[0m

Next steps:
  1. edit secrets:        sudo nano ${INSTALL_DIR}/.env
  2. restart service:     sudo systemctl restart qt
  3. tail logs:           sudo journalctl -u qt -f
  4. dashboard:           http://<your-lighthouse-ip>:8765
                          (open port 8765 in the Lighthouse firewall)

To upgrade later, just re-run this same one-liner.
EOF
