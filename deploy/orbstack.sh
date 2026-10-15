#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTEXT="${QT_DOCKER_CONTEXT:-orbstack}"
PROJECT="${QT_ORBSTACK_PROJECT_NAME:-qt-orbstack}"
PRIVATE_ENV="${QT_ORBSTACK_ENV_FILE:-${ROOT}/.env.orbstack}"
PYTHON="${PYTHON:-python3}"
DOCKER=(docker --context "$CONTEXT")
COMPOSE=("${DOCKER[@]}" compose --project-name "$PROJECT" --project-directory "$ROOT" --env-file "$PRIVATE_ENV" -f "$ROOT/docker-compose.orbstack.yml")

# Shell settings must not silently override the inspected private configuration.
for key in QT_ORBSTACK_PORT QT_ORBSTACK_USERNAME QT_ORBSTACK_PASSWORD_HASH \
  QT_WORKBENCH_ORIGIN_CLIENT_ID QT_WORKBENCH_ORIGIN_CLIENT_SECRET \
  QT_BTC_MONITOR_VENUE QT_BTC_MONITOR_ENVIRONMENT QT_TELEGRAM_BOT_TOKEN QT_TELEGRAM_CHAT_ID; do
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
  down) "${COMPOSE[@]}" down ;;
  *) echo 'Usage: deploy/orbstack.sh [init|up|check|status|down]' >&2; exit 2 ;;
esac
