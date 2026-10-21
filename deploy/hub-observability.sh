#!/usr/bin/env bash
# Optional local Langfuse; isolated Compose project and volumes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OBS_ENV="${QT_AGENT_HUB_OBSERVABILITY_ENV_FILE:-${ROOT}/.env.agent-hub-observability}"
PYTHON="${PYTHON:-python3}"
if [[ "${1:-}" == check || "${1:-}" == replay ]] && [[ "$PYTHON" == python3 && -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
fi
COMPOSE=(docker --context orbstack compose --project-name qt-agent-hub-observability --project-directory "$ROOT" --env-file "$OBS_ENV" -f "$ROOT/docker-compose.agent-hub-observability.yml")
case "${1:-}" in
  init) "$PYTHON" "$ROOT/deploy/hub_observability.py" init --root "$ROOT" --env-file "$OBS_ENV" ;;
  connect) "$PYTHON" "$ROOT/deploy/hub_observability.py" connect --root "$ROOT" --env-file "$OBS_ENV" ;;
  up) "${COMPOSE[@]}" up -d --wait --wait-timeout 300 ;;
  status) "${COMPOSE[@]}" ps ;;
  config) "${COMPOSE[@]}" config --quiet ;;
  stop) "${COMPOSE[@]}" stop ;;
  check) "$PYTHON" "$ROOT/deploy/hub_observability.py" check --root "$ROOT" --env-file "$OBS_ENV" ;;
  replay) "$PYTHON" "$ROOT/deploy/hub_observability.py" replay --run-id "${2:?Supply a completed run ID}" --root "$ROOT" --env-file "$OBS_ENV" ;;
  ui-check) "${COMPOSE[@]}" --profile test run --build --rm -T --no-deps ui-test "${2:?Supply a trace ID}" ;;
  logs) "$PYTHON" "$ROOT/deploy/hub_observability.py" logs --root "$ROOT" --env-file "$OBS_ENV" ;;
  *) echo 'Use init|up|connect|status|check|replay RUN_ID|ui-check TRACE_ID|logs|config|stop. Credentials stay in the ignored local env file.' >&2; exit 2 ;;
esac
