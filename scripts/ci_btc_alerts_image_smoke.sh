#!/usr/bin/env bash
set -euo pipefail
image="${1:?Provide the already-built alert image}"
if [[ "$(uname -s)" == Darwin ]]; then
  context="${QT_DOCKER_CONTEXT:-orbstack}"
  [[ "$context" == orbstack ]] || { echo 'Local smoke requires OrbStack.' >&2; exit 2; }
else
  context="${QT_DOCKER_CONTEXT:-default}"
fi
docker --context "$context" run --rm --network none --read-only --user 10001:10001 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:size=16m,mode=1777 --tmpfs /state:size=32m,uid=10001,gid=10001,mode=0700 \
  -i --entrypoint python "$image" - <<'PY'
import importlib.util
from pathlib import Path
from qt.btc_alerts.models import Policy
from qt.btc_alerts.store import Store
assert importlib.util.find_spec("qt.btc_agent") is None
assert importlib.util.find_spec("qt.execution") is None
store = Store(Path("/state"))
revision, policy = store.policy()
assert policy.strategy_version == Policy.model_validate_json(policy.model_dump_json()).strategy_version
first = store.request_test("offline-fixture-one", 10000, "ci")
assert store.claim(first, "fixture-recipient-btc", 10000)
store.recover(10001)
second = store.request_test("offline-fixture-two", 10001, "ci")
assert not store.claim(second, "fixture-recipient-btc", 10001)
assert store.event(first)["notification"]["status"] == "unknown"
assert store.policy()[0] == revision
print("Offline alert ledger, restart quota and image isolation passed.")
PY
