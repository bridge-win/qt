#!/usr/bin/env bash
# Idempotent server-side deploy for rsynced QT checkouts.
#
# This script is intended to be run as root on the Aliyun host after
# deploy/ssh-deploy.sh mirrors the local tree to /opt/qt.

set -Eeuo pipefail

INSTALL_DIR="${QT_INSTALL_DIR:-/opt/qt}"
SERVICE_USER="${QT_USER:-qt}"
WEB_PORT="${QT_DASHBOARD_PORT:-8765}"
DOCKER_CADDY_CONTAINER="${QT_DOCKER_CADDY_CONTAINER:-kol-caddy}"
DOCKER_CADDYFILE="${QT_DOCKER_CADDYFILE:-/srv/kol/deploy/Caddyfile}"

log() { printf '[qt-deploy] %s\n' "$*"; }
die() { printf '[qt-deploy] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root."
[[ -d "${INSTALL_DIR}" ]] || die "Install directory does not exist: ${INSTALL_DIR}"
[[ -f "${INSTALL_DIR}/pyproject.toml" ]] || die "Missing pyproject.toml in ${INSTALL_DIR}"

export DEBIAN_FRONTEND=noninteractive

log "installing system packages"
apt-get update -y
apt-get install -y --no-install-recommends \
  curl ca-certificates build-essential caddy openssl \
  python3 python3-venv python3-pip python3-dev \
  pkg-config libssl-dev

log "preparing service user and directories"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

mkdir -p "${INSTALL_DIR}/data/runtime" "${INSTALL_DIR}/data/backtests"

DASHBOARD_HOST=127.0.0.1
USE_DOCKER_CADDY=false
if command -v docker >/dev/null 2>&1 \
  && docker inspect "${DOCKER_CADDY_CONTAINER}" >/dev/null 2>&1 \
  && [[ -f "${DOCKER_CADDYFILE}" ]]; then
  DASHBOARD_HOST="$(
    docker inspect "${DOCKER_CADDY_CONTAINER}" \
      --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
  )"
  [[ -n "${DASHBOARD_HOST}" ]] \
    || die "could not determine the Docker Caddy gateway"
  USE_DOCKER_CADDY=true
  log "using existing Docker Caddy via ${DASHBOARD_HOST}:${WEB_PORT}"
fi

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
sed "s/--dashboard-host 127\\.0\\.0\\.1/--dashboard-host ${DASHBOARD_HOST}/" \
  "${INSTALL_DIR}/deploy/qt.service" \
  >/tmp/qt.service
install -m 0644 /tmp/qt.service /etc/systemd/system/qt.service
rm -f /tmp/qt.service
install -m 0644 \
  "${INSTALL_DIR}/deploy/qt-research-worker.service" \
  /etc/systemd/system/qt-research-worker.service
install -m 0644 \
  "${INSTALL_DIR}/deploy/qt-research-data-refresh.service" \
  /etc/systemd/system/qt-research-data-refresh.service
install -m 0644 \
  "${INSTALL_DIR}/deploy/qt-research-data-refresh.timer" \
  /etc/systemd/system/qt-research-data-refresh.timer
systemctl daemon-reload
systemctl enable \
  qt.service \
  qt-research-worker.service \
  qt-research-data-refresh.timer
systemctl restart qt.service
systemctl restart qt-research-worker.service
systemctl restart qt-research-data-refresh.timer
systemctl start qt-research-data-refresh.service

log "configuring authenticated Caddy HTTPS"
install -d -m 0700 /etc/qt
AUTH_FILE=/etc/qt/research-auth
if [[ ! -f "${AUTH_FILE}" ]]; then
  PREVIOUS_UMASK="$(umask)"
  umask 077
  printf 'qt:%s\n' "$(openssl rand -base64 24 | tr -d '\n')" >"${AUTH_FILE}"
  umask "${PREVIOUS_UMASK}"
fi
RESEARCH_PASSWORD="$(cut -d: -f2- "${AUTH_FILE}")"
if [[ "${USE_DOCKER_CADDY}" == true ]]; then
  RESEARCH_HASH="$(
    docker exec "${DOCKER_CADDY_CONTAINER}" \
      caddy hash-password --plaintext "${RESEARCH_PASSWORD}"
  )"
  sed \
    -e "s|{\$QT_RESEARCH_PASSWORD_HASH}|${RESEARCH_HASH}|" \
    -e "s|127\\.0\\.0\\.1:${WEB_PORT}|${DASHBOARD_HOST}:${WEB_PORT}|" \
    "${INSTALL_DIR}/deploy/qt.caddy" \
    >/tmp/qt.caddy
  awk '
    $0 == "# BEGIN QT RESEARCH" { skip = 1; next }
    $0 == "# END QT RESEARCH" { skip = 0; next }
    !skip { print }
  ' "${DOCKER_CADDYFILE}" >/tmp/Caddyfile.qt
  {
    cat /tmp/Caddyfile.qt
    printf '\n# BEGIN QT RESEARCH\n'
    cat /tmp/qt.caddy
    printf '# END QT RESEARCH\n'
  } >"${DOCKER_CADDYFILE}"
  rm -f /tmp/Caddyfile.qt /tmp/qt.caddy
  docker exec "${DOCKER_CADDY_CONTAINER}" \
    caddy validate --config /etc/caddy/Caddyfile
  docker exec "${DOCKER_CADDY_CONTAINER}" \
    caddy reload --config /etc/caddy/Caddyfile
  systemctl disable --now caddy >/dev/null 2>&1 || true
else
  RESEARCH_HASH="$(caddy hash-password --plaintext "${RESEARCH_PASSWORD}")"
  install -d -m 0755 /etc/caddy/sites
  sed "s|{\$QT_RESEARCH_PASSWORD_HASH}|${RESEARCH_HASH}|" \
    "${INSTALL_DIR}/deploy/qt.caddy" \
    >/tmp/qt.caddy
  install -m 0644 /tmp/qt.caddy /etc/caddy/sites/qt.caddy
  rm -f /tmp/qt.caddy
  if ! grep -Fq 'import /etc/caddy/sites/*.caddy' /etc/caddy/Caddyfile; then
    printf '\nimport /etc/caddy/sites/*.caddy\n' >>/etc/caddy/Caddyfile
  fi
  if ! caddy validate --config /etc/caddy/Caddyfile; then
    sed -i 's/^[[:space:]]*basic_auth /    basicauth /' \
      /etc/caddy/sites/qt.caddy
    caddy validate --config /etc/caddy/Caddyfile
  fi
  systemctl enable --now caddy
  systemctl reload caddy
fi

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp || true
  ufw allow 443/tcp || true
fi

log "checking local dashboard"
for attempt in {1..30}; do
  if curl -fsS -o /dev/null -w "[qt-deploy] dashboard: %{http_code}\n" "http://${DASHBOARD_HOST}:${WEB_PORT}/"; then
    break
  fi
  if [[ "${attempt}" -eq 30 ]]; then
    systemctl --no-pager --full status qt.service || true
    die "dashboard did not become ready on ${DASHBOARD_HOST}:${WEB_PORT}"
  fi
  sleep 1
done
systemctl --no-pager --full status qt.service | sed -n '1,18p'
systemctl --no-pager --full status qt-research-worker.service | sed -n '1,18p'

log "checking authenticated public HTTPS"
for attempt in {1..30}; do
  if curl -fsS --user "$(cat "${AUTH_FILE}")" \
    -o /dev/null -w "[qt-deploy] public HTTPS: %{http_code}\n" \
    "https://qt.followkol.live/api/v2/backtests/health"; then
    break
  fi
  if [[ "${attempt}" -eq 30 ]]; then
    journalctl -u caddy -n 40 --no-pager || true
    die "authenticated HTTPS did not become ready"
  fi
  sleep 2
done
