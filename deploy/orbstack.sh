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

# Production V4 owns separate database/data volumes and a separate Compose
# project. This branch never restarts the existing workbench or Agent Hub.
if [[ "${1:-}" == v4-* ]]; then
  V4_ENV="${QT_V4_ENV_FILE:-${ROOT}/.env.production-v4}"
  # Compose gives exported shell values precedence over --env-file. Run only
  # with the inspected private settings, including the database destination.
  while IFS= read -r v4_setting; do
    case "$v4_setting" in
      QT_V4_*|QT_EXPERIMENT_IMAGE_DIGEST) unset "$v4_setting" ;;
    esac
  done < <(compgen -e)
  if [[ "$1" == v4-init ]]; then
    "$PYTHON" "$ROOT/deploy/v4_config.py" init --env-file "$V4_ENV"
    exit
  fi
  "$PYTHON" "$ROOT/deploy/v4_config.py" check --env-file "$V4_ENV"
  V4=("${DOCKER[@]}" compose --project-name qt-production-v4 --project-directory "$ROOT" --env-file "$V4_ENV" -f "$ROOT/deploy/v4-compose.yml")
  V4_TEST_DB=("${DOCKER[@]}" compose --project-name qt-v4-test-db --project-directory "$ROOT" --env-file "$V4_ENV" -f "$ROOT/deploy/v4-test-db.yml")
  case "$1" in
    v4-config) "${V4[@]}" --profile test --profile candidate config --quiet ;;
    v4-up) "${V4[@]}" config --quiet; "${V4[@]}" up --build -d --wait --wait-timeout 240 ;;
    v4-build) shift; "${V4[@]}" build "$@" ;;
    v4-status) "${V4[@]}" ps ;;
    v4-check) "${V4[@]}" exec -T control-api python -m qt.control check ;;
    v4-exec) shift; "${V4[@]}" exec -T "$@" ;;
    v4-logs) "${V4[@]}" logs --tail 80 "${2:-control-api}" ;;
    v4-stop) shift; "${V4[@]}" stop "$@" ;;
    v4-start) shift; "${V4[@]}" start "$@" ;;
    v4-down) "${V4[@]}" down ;;
    v4-test)
      shift
      "${V4[@]}" --profile test build test
      "${V4[@]}" --profile test run --rm -T --no-deps test "$@"
      ;;
    v4-test-db-up) "${V4_TEST_DB[@]}" up -d --wait --wait-timeout 120 ;;
    v4-test-db-down) "${V4_TEST_DB[@]}" down ;;
    v4-test-postgres)
      shift
      "${V4_TEST_DB[@]}" up -d --wait --wait-timeout 120
      V4_TEST_PYTHON="$PYTHON"
      [[ ! -x "$ROOT/.venv/bin/python" ]] || V4_TEST_PYTHON="$ROOT/.venv/bin/python"
      "$V4_TEST_PYTHON" "$ROOT/deploy/v4_postgres_tests.py" --env-file "$V4_ENV" -- "$@"
      ;;
    v4-backup) "$PYTHON" "$ROOT/deploy/v4_backup.py" backup --root "$ROOT" --env-file "$V4_ENV" ;;
    v4-restore-check)
      [[ -n "${2:-}" ]] || { echo 'Provide a trusted local V4 .dump backup path.' >&2; exit 2; }
      "${V4_TEST_DB[@]}" up -d --wait --wait-timeout 120
      "$PYTHON" "$ROOT/deploy/v4_backup.py" restore-check --root "$ROOT" --env-file "$V4_ENV" --path "$2"
      ;;
    v4-native-compare)
      "${V4[@]}" --profile candidate build native-baseline-check native-candidate
      "$PYTHON" "$ROOT/deploy/v4_native_upgrade.py" --root "$ROOT" --env-file "$V4_ENV"
      ;;
    *) echo 'Use v4-init|v4-config|v4-up|v4-build|v4-status|v4-check|v4-exec|v4-logs|v4-stop|v4-start|v4-down|v4-test|v4-test-db-up|v4-test-db-down|v4-test-postgres [pytest args]|v4-backup|v4-restore-check PATH|v4-native-compare' >&2; exit 2 ;;
  esac
  exit
fi

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
    hub-observability) shift; exec "$ROOT/deploy/hub-observability.sh" "$@" ;;
    hub-up) "${HUB[@]}" up --build -d --wait --wait-timeout 240 ;;
    hub-status) "${HUB[@]}" ps ;;
    hub-check) "${HUB[@]}" exec -T api python -m qt.agent_hub.verify ;;
    hub-test) shift; "${HUB[@]}" --profile test build test; "${HUB[@]}" --profile test run --rm -T --no-deps test "$@" ;;
    hub-native-test) shift; "${HUB[@]}" --profile test build research-test; "${HUB[@]}" --profile test run --rm -T --no-deps research-test --native "$@" ;;
    hub-ui-test) shift; "${HUB[@]}" --profile test build ui-test; "${HUB[@]}" --profile test run --rm -T --no-deps ui-test "$@" ;;
    hub-build) shift; "${HUB[@]}" build "$@" ;;
    hub-config) "${HUB[@]}" config --quiet ;;
    hub-exec) shift; "${HUB[@]}" exec -T "$@" ;;
    hub-logs) "${HUB[@]}" logs --tail 80 "${2:-worker-1}" ;;
    hub-restart) "${HUB[@]}" restart "${2:-worker-1}" ;;
    hub-stop) shift; "${HUB[@]}" stop "$@" ;;
    hub-start) shift; "${HUB[@]}" start "$@" ;;
    hub-down) "${HUB[@]}" down ;;
    *) echo 'Use hub-init|hub-up|hub-status|hub-test [pytest args]|hub-native-test [pytest args]|hub-ui-test|hub-observability init/up/connect/status/check/stop|hub-build [service...]|hub-config|hub-check|hub-exec SERVICE COMMAND...|hub-logs [service]|hub-restart [service]|hub-stop [service...]|hub-start [service...]|hub-down' >&2; exit 2 ;;
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
