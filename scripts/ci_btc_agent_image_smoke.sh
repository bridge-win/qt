#!/usr/bin/env bash
# No credentials, public network, Compose project, or existing state is used.
set -euo pipefail
image="${1:-qt-btc-agent:ci}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
volume="qt-btc-agent-acceptance-$(date +%s)-$$"
cleanup() { docker volume rm "$volume" >/dev/null; }
docker volume create "$volume" >/dev/null
trap cleanup EXIT
args=(--rm --network none --read-only --cap-drop ALL
      --security-opt no-new-privileges:true --tmpfs /tmp:size=32m,mode=1777
      --mount "type=volume,src=$volume,dst=/state"
      --mount "type=bind,src=$root/tests/fixtures/btc_agent_image_recovery.py,dst=/acceptance.py,readonly"
      --entrypoint python)
set +e
docker run "${args[@]}" "$image" /acceptance.py crash /state
code=$?
set -e
if [[ "$code" != 72 ]]; then
    echo "Expected deliberate crash exit 72, got $code" >&2
    exit 1
fi
docker run "${args[@]}" "$image" /acceptance.py recover /state
