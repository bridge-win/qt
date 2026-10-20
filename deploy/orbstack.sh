#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTEXT="${QT_DOCKER_CONTEXT:-orbstack}"
[[ "$CONTEXT" == orbstack ]] || { echo 'Local QT containers require --context orbstack.' >&2; exit 2; }
PROJECT="${QT_ORBSTACK_PROJECT_NAME:-qt-orbstack}"
PRIVATE_ENV="${QT_ORBSTACK_ENV_FILE:-${ROOT}/.env.orbstack}"
PYTHON="${PYTHON:-python3}"
DOCKER=(docker --context "$CONTEXT")
COMPOSE=("${DOCKER[@]}" compose --project-name "$PROJECT" --project-directory "$ROOT" --env-file "$PRIVATE_ENV" -f "$ROOT/docker-compose.orbstack.yml")

# The additive Agent Hub has independent volumes, credentials and lifecycle.
# It never restarts the existing workbench or another project's containers.
if [[ "${1:-}" == hub-* ]]; then
  HUB_ENV="${QT_AGENT_HUB_ENV_FILE:-${ROOT}/.env.agent-hub}"
  HUB=("${DOCKER[@]}" compose --project-name qt-agent-hub --project-directory "$ROOT" --env-file "$HUB_ENV" -f "$ROOT/docker-compose.agent-hub.yml")
  if [[ "$1" == hub-init ]]; then
    "$PYTHON" "$ROOT/deploy/agent_hub_config.py" init --env-file "$HUB_ENV" --root "$ROOT"
    exit
  fi
  "$PYTHON" "$ROOT/deploy/agent_hub_config.py" check --env-file "$HUB_ENV" --root "$ROOT"
  "${DOCKER[@]}" info --format '{{.OSType}}/{{.Architecture}}' >/dev/null
  "${HUB[@]}" config --quiet
  case "$1" in
    hub-up) "${HUB[@]}" up --build -d --wait --wait-timeout 240 ;;
    hub-status) "${HUB[@]}" ps ;;
    hub-check) "${HUB[@]}" exec -T api python -m qt.agent_hub.verify ;;
    hub-test) "${HUB[@]}" --profile test build test; "${HUB[@]}" --profile test run --rm -T --no-deps test ;;
    hub-exec) shift; "${HUB[@]}" exec -T "$@" ;;
    hub-logs) "${HUB[@]}" logs --tail 80 "${2:-worker-1}" ;;
    hub-restart) "${HUB[@]}" restart "${2:-worker-1}" ;;
    hub-down) "${HUB[@]}" down ;;
    *) echo 'Use hub-init|hub-up|hub-status|hub-test|hub-check|hub-exec SERVICE COMMAND...|hub-logs [service]|hub-restart [service]|hub-down' >&2; exit 2 ;;
  esac
  exit
fi

# Shell settings must not silently override the inspected private configuration.
for key in QT_ORBSTACK_PORT QT_ORBSTACK_USERNAME QT_ORBSTACK_PASSWORD_HASH \
  QT_WORKBENCH_ORIGIN_CLIENT_ID QT_WORKBENCH_ORIGIN_CLIENT_SECRET \
  QT_BTC_MONITOR_VENUE QT_BTC_MONITOR_ENVIRONMENT QT_TELEGRAM_BOT_TOKEN QT_TELEGRAM_CHAT_ID \
  CODEX_API_KEY QT_BTC_ALERTS_CODEX_MODEL QT_BTC_ALERTS_REVIEW_SECRET QT_BTC_ALERTS_EXPECTED_BOT; do
  unset "$key"
done

config_check() {
  "$PYTHON" "$ROOT/deploy/orbstack_config.py" check --env-file "$PRIVATE_ENV"
  "${DOCKER[@]}" info --format '{{.OSType}}/{{.Architecture}}' >/dev/null
  "${COMPOSE[@]}" config --quiet
}

case "${1:-up}" in
  init)
    "${DOCKER[@]}" info --format '{{.OSType}}/{{.Architecture}}' >/dev/null
    "$PYTHON" "$ROOT/deploy/orbstack_config.py" init --env-file "$PRIVATE_ENV" --context "$CONTEXT"
    ;;
  up)
    "$PYTHON" "$ROOT/deploy/orbstack_config.py" alerts-bootstrap --env-file "$PRIVATE_ENV"
    config_check
    "${COMPOSE[@]}" up --build -d --wait --wait-timeout 240
    port="$("$PYTHON" "$ROOT/deploy/orbstack_config.py" port --env-file "$PRIVATE_ENV")"
    printf 'Workbench: http://127.0.0.1:%s (use the local login you created).\n' "$port"
    ;;
  check)
    config_check
    "${COMPOSE[@]}" exec -T api python deploy/workbench_health.py api
    "${COMPOSE[@]}" exec -T research-worker python deploy/workbench_health.py worker
    port="$("$PYTHON" "$ROOT/deploy/orbstack_config.py" port --env-file "$PRIVATE_ENV")"
    for path in / /api/v3/experiments; do
      code="$(curl --max-time 10 --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${port}${path}")"
      [[ "$code" == 401 ]] || { echo 'Unauthenticated ingress did not fail closed.' >&2; exit 1; }
    done
    echo 'Local API/worker probes passed; unauthenticated UI/API requests were rejected.'
    ;;
  status) "${COMPOSE[@]}" ps ;;
  alerts-init) "$PYTHON" "$ROOT/deploy/orbstack_config.py" alerts-init --env-file "$PRIVATE_ENV" ;;
  alerts-check) config_check; "${COMPOSE[@]}" exec -T alerts python -m qt.btc_alerts doctor ;;
  alerts-mode)
    [[ "${2:-}" =~ ^(off|shadow|notify)$ ]] || { echo 'Choose off, shadow or notify.' >&2; exit 2; }
    "${COMPOSE[@]}" exec -T alerts python -m qt.btc_alerts mode --mode "$2"
    ;;
  alerts-verify)
    [[ -n "${2:-}" ]] || { echo 'Provide the QT VERIFY challenge sent from your private Telegram chat.' >&2; exit 2; }
    "${COMPOSE[@]}" exec -T alerts python -m qt.btc_alerts verify-recipient --nonce "$2"
    ;;
  alerts-test) "${COMPOSE[@]}" exec -T alerts python -m qt.btc_alerts test-notification ;;
  alerts-research) "${COMPOSE[@]}" run --rm --no-deps -T alerts research ;;
  down) "${COMPOSE[@]}" down ;;
  *) echo 'Usage: deploy/orbstack.sh [init|up|check|status|down|alerts-init|alerts-check|alerts-mode MODE|alerts-verify CHALLENGE|alerts-test|alerts-research]' >&2; exit 2 ;;
esac
