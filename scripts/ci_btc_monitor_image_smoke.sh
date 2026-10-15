#!/usr/bin/env bash
# Actual image verification on CI's Docker runner. Never contact market/Telegram APIs.
set -euo pipefail
image=${1:-qt-btc-monitor:ci}
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fixture="$root/tests/fixtures/btc_monitor_image_worker.py"
temporary=$(mktemp -d /tmp/qt-btc-monitor-image.XXXXXX)
state="$temporary/state"
container="qt-btc-monitor-smoke-${GITHUB_RUN_ID:-local}-$$"
read_gid=$(id -g)
cleanup() {
  result=$?
  if [ "$result" -ne 0 ]; then
    docker logs "$container" 2>/dev/null || true
  fi
  docker rm --force "$container" >/dev/null 2>&1 || true
  case "$temporary" in
    /tmp/qt-btc-monitor-image.*) sudo rm -rf -- "$temporary" ;;
  esac
  exit "$result"
}
trap cleanup EXIT
sudo install -d -o 10001 -g "$read_gid" -m 2750 "$state"
hardening=(--network none --init --read-only --user 10001:10001
  --cap-drop ALL --security-opt no-new-privileges:true
  --tmpfs /tmp:rw,size=32m,mode=1777 --memory 384m --cpus 1 --pids-limit 64
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3
  --env QT_TELEGRAM_BOT_TOKEN= --env QT_TELEGRAM_CHAT_ID=)

echo "Image dependencies and isolated package boundary"
docker run --rm "${hardening[@]}" --entrypoint python "$image" -c '
import importlib.metadata as m, importlib.util as u, json, sqlite3, sys
assert u.find_spec("fastapi") is None and u.find_spec("qt.btc_agent") is None
print(json.dumps({"python": sys.version, "sqlite": sqlite3.sqlite_version,
                 "dependencies": {name: m.version(name) for name in ("httpx", "pydantic", "websockets")}}))'

echo "Credential-free default-entrypoint self-test without network or writable state"
docker run --rm "${hardening[@]}" \
  --env QT_BTC_MONITOR_CONFIG=/must-not-read.json \
  --env QT_BTC_MONITOR_STATE_DIR=/must-not-write \
  "$image" self-test --state-dir /must-not-write

echo "Start real service with read-only offline fixture and API-readable host state"
docker run --detach --name "$container" --restart unless-stopped "${hardening[@]}" \
  --group-add "$read_gid" \
  --mount "type=bind,source=$state,target=/state" \
  --mount "type=bind,source=$fixture,target=/smoke/worker.py,readonly" \
  --health-cmd 'python -m qt.btc_monitor health --state-dir /state' \
  --health-interval 1s --health-timeout 5s --health-start-period 3s --health-retries 2 \
  --entrypoint python "$image" /smoke/worker.py --state-dir /state >/dev/null
wait_health() {
  target=$1
  for ((attempt=0; attempt<30; attempt++)); do
    actual=$(docker inspect --format '{{.State.Health.Status}}' "$container")
    if [ "$actual" = "$target" ]; then return 0; fi
    if [ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]; then
      echo "Worker exited before health=$target" >&2; return 1
    fi
    sleep 1
  done
  echo "Timed out waiting for health=$target (last=$actual)" >&2
  return 1
}
wait_health healthy
docker exec "$container" python -m qt.btc_monitor health --state-dir /state
# Micro readiness is independent of the much longer production grid warmup.
for ((attempt=0; attempt<20; attempt++)); do
  if docker exec "$container" python -c '
from pathlib import Path
from qt.btc_monitor.service import read_status
s=read_status(Path("/state"))
assert s.health.data_ready and not s.live_execution_enabled
assert s.health.channels["trade"].ready and s.health.channels["quote"].ready
'; then break; fi
  if [ "$attempt" -eq 19 ]; then exit 1; fi
  sleep 1
done
# The separate UID has only the workbench API group and a read-only bind.
docker run --rm --network none --read-only --user "20002:$read_gid" \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --mount "type=bind,source=$state,target=/state,readonly" \
  "$image" status --state-dir /state > "$temporary/status.json"
python3 - "$temporary/status.json" "$state/latest.json" "$read_gid" <<'PY'
import json, stat, sys
from pathlib import Path
snapshot=json.loads(Path(sys.argv[1]).read_text())
info=Path(sys.argv[2]).stat()
assert snapshot["live_execution_enabled"] is False
assert info.st_uid==10001 and info.st_gid==int(sys.argv[3])
assert stat.S_IMODE(info.st_mode)==0o640
print("Read-only API-group access and snapshot 0640 ownership verified")
PY

echo "Paused core must become unhealthy; external health status alone does not restart it"
docker exec "$container" python -c 'import os,signal; from pathlib import Path; os.kill(int(Path("/state/smoke-worker.pid").read_text()),signal.SIGSTOP)'
wait_health unhealthy
if docker exec "$container" python -m qt.btc_monitor health --state-dir /state; then
  echo "Stale core incorrectly passed health" >&2; exit 1
fi
docker exec "$container" python -c 'import os,signal; from pathlib import Path; os.kill(int(Path("/state/smoke-worker.pid").read_text()),signal.SIGCONT)'
wait_health healthy
test "$(docker inspect --format '{{.RestartCount}}' "$container")" = 0

echo "SIGTERM must stop cleanly and persist stopped liveness"
docker stop --time 20 "$container" >/dev/null
test "$(docker inspect --format '{{.State.ExitCode}}' "$container")" = 0
test "$(docker inspect --format '{{.State.OOMKilled}}' "$container")" = false
docker run --rm --network none --read-only --user "20002:$read_gid" \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --mount "type=bind,source=$state,target=/state,readonly" \
  "$image" status --state-dir /state > "$temporary/stopped.json"
python3 - "$temporary/stopped.json" <<'PY'
import json, sys
from pathlib import Path
snapshot=json.loads(Path(sys.argv[1]).read_text())
assert snapshot["health"]["alive"] is False
assert snapshot["health"]["status"]=="stopped"
assert snapshot["live_execution_enabled"] is False
PY
if docker run --rm --network none --read-only --user "20002:$read_gid" \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --mount "type=bind,source=$state,target=/state,readonly" \
  "$image" health --state-dir /state; then
  echo "Stopped worker incorrectly passed health" >&2; exit 1
fi
echo "Offline hardened image lifecycle smoke passed"
