"""Private local Langfuse provisioning and real OTLP round-trip acceptance."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("private_regular_configuration_required")
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, raw = line.split("=", 1)
        value = shlex.split(raw)
        if len(value) != 1 or not key.startswith(("QT_AGENT_HUB_", "QT_LANGFUSE_")):
            raise ValueError("invalid_private_configuration")
        values[key] = value[0]
    return values


def save_env(path: Path, values: dict[str, str]) -> None:
    if path.is_symlink():
        raise ValueError("private_regular_configuration_required")
    fd, temporary = tempfile.mkstemp(prefix=".env.observability-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(
                "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "connect", "check", "replay", "logs"))
    parser.add_argument("--run-id")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    path = args.env_file
    if args.command == "init":
        if path.exists():
            read_env(path)
            print("Local observability configuration already exists; preserved.")
            return 0
        values = {
            f"QT_LANGFUSE_{name}": secrets.token_hex(32)
            for name in (
                "POSTGRES_PASSWORD",
                "CLICKHOUSE_PASSWORD",
                "REDIS_PASSWORD",
                "MINIO_PASSWORD",
                "SALT",
                "ENCRYPTION_KEY",
                "AUTH_SECRET",
                "USER_PASSWORD",
            )
        }
        values.update(
            {
                "QT_AGENT_HUB_LANGFUSE_PUBLIC_KEY": "pk-lf-" + secrets.token_hex(24),
                "QT_AGENT_HUB_LANGFUSE_SECRET_KEY": "sk-lf-" + secrets.token_hex(32),
                "QT_LANGFUSE_PORT": "3080",
                "QT_LANGFUSE_USER_EMAIL": "owner@qt.local",
            }
        )
        save_env(path, values)
        print(
            f"Created private configuration: {path.name}; user owner@qt.local. No secrets printed."
        )
        return 0
    values = read_env(path)
    if args.command == "logs":
        result = subprocess.run(
            [
                "docker",
                "--context",
                "orbstack",
                "compose",
                "--project-name",
                "qt-agent-hub-observability",
                "--project-directory",
                str(args.root),
                "--env-file",
                str(path),
                "-f",
                str(args.root / "docker-compose.agent-hub-observability.yml"),
                "logs",
                "--no-color",
                "--tail",
                "40",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        output = result.stdout + result.stderr
        for key, value in values.items():
            if len(value) >= 16 or key.endswith("USER_EMAIL"):
                output = output.replace(value, "[redacted]")
        combined = (
            values["QT_AGENT_HUB_LANGFUSE_PUBLIC_KEY"]
            + ":"
            + values["QT_AGENT_HUB_LANGFUSE_SECRET_KEY"]
        )
        output = output.replace(base64.b64encode(combined.encode()).decode(), "[redacted]")
        print(output, end="")
        return result.returncode
    if args.command == "connect":
        hub = Path(os.environ.get("QT_AGENT_HUB_ENV_FILE", str(args.root / ".env.agent-hub")))
        if not hub.is_file():
            raise ValueError("hub_configuration_missing_run_hub_init")
        current = read_env(hub)
        current.update(
            {
                key: values[key]
                for key in ("QT_AGENT_HUB_LANGFUSE_PUBLIC_KEY", "QT_AGENT_HUB_LANGFUSE_SECRET_KEY")
            }
        )
        current["QT_AGENT_HUB_OTLP_ENDPOINT"] = "http://langfuse-web:3000/api/public/otel"
        save_env(hub, current)
        print("Hub OTLP configuration connected. Run deploy/orbstack.sh hub-up to apply.")
        return 0

    sys.path.insert(0, str(args.root / "src"))
    from qt.agent_hub.governance import Governance
    from qt.agent_hub.observability import export_span, flush

    base = f"http://127.0.0.1:{int(values['QT_LANGFUSE_PORT'])}"
    for key in ("QT_AGENT_HUB_LANGFUSE_PUBLIC_KEY", "QT_AGENT_HUB_LANGFUSE_SECRET_KEY"):
        os.environ[key] = values[key]
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = base + "/api/public/otel"
    auth = base64.b64encode(
        f"{values['QT_AGENT_HUB_LANGFUSE_PUBLIC_KEY']}:{values['QT_AGENT_HUB_LANGFUSE_SECRET_KEY']}".encode()
    ).decode()
    run = "observability-check-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory() as folder:
        if args.command == "replay":
            if not args.run_id or len(args.run_id) > 128:
                raise ValueError("completed_run_id_required")
            hub = read_env(
                Path(os.environ.get("QT_AGENT_HUB_ENV_FILE", str(args.root / ".env.agent-hub")))
            )
            hub_url = f"http://127.0.0.1:{int(hub['QT_AGENT_HUB_PORT'])}"
            query = urllib.parse.urlencode({"run_id": args.run_id, "limit": 1000})
            headers = {"Authorization": "Bearer " + hub["QT_AGENT_HUB_SERVICE_TOKEN"]}
            request = urllib.request.Request(hub_url + "/api/spans?" + query, headers=headers)
            with urllib.request.urlopen(request, timeout=10) as response:
                local = json.load(response)["items"]
            request = urllib.request.Request(
                hub_url + "/api/runs/" + urllib.parse.quote(args.run_id, safe="") + "/diagnostics",
                headers=headers,
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                diagnostics = json.load(response)
            if not local or len(local) != diagnostics["span_count"] or diagnostics["incomplete"]:
                raise RuntimeError("replay_requires_complete_untruncated_run")
            for index, row in enumerate(local):
                if not export_span(row):
                    raise RuntimeError("otlp_replay_enqueue_failed")
                if index % 32 == 31 and not flush():
                    raise RuntimeError("otlp_replay_flush_failed")
        else:
            ledger = Governance(Path(folder))
            with ledger.invocation(
                "personal", run, run, "monitor", attempt=1, worker_id="acceptance"
            ):
                ledger.call("retrieval", "context.search", lambda: {"ready": True})
                ledger.call(
                    "model",
                    "codex.acceptance",
                    lambda: {"_runtime": {"usage": {"input_tokens": 100, "output_tokens": 20}}},
                    metadata={"model": "synthetic-acceptance", "provider": "test-fixture"},
                )
            local = ledger.spans("personal", run)
        identity = local[0]["trace_id"]
        if not flush():
            raise RuntimeError("otlp_flush_failed")
        url = (
            base
            + "/api/public/v2/observations?traceId="
            + identity
            + "&limit=1000&fields=core,basic,usage,model"
        )
        deadline = time.monotonic() + 55
        rows = []
        while time.monotonic() < deadline:
            request = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    rows = json.load(response).get("data", [])
            except urllib.error.HTTPError as error:
                if error.code not in {404, 503}:
                    raise RuntimeError(f"observation_query_http_{error.code}") from None
            if {row["id"] for row in rows} >= {row["span_id"] for row in local}:
                break
            time.sleep(2)
        persisted = {row["id"]: row for row in rows}
        if not all(row["span_id"] in persisted for row in local):
            raise RuntimeError("otlp_readback_missing_spans")
        for row in local:
            remote = persisted[row["span_id"]]
            if (remote.get("parentObservationId") or None) != row["parent_span_id"]:
                raise RuntimeError("otlp_readback_parent_mismatch")
            if row["operation"] == "model":
                if str(remote.get("type", "")).lower() != "generation":
                    raise RuntimeError("otlp_readback_generation_type_mismatch")
                usage = remote.get("usageDetails", {})
                expected = row["metadata"].get("usage", {})
                for key in ("input_tokens", "output_tokens"):
                    if key in expected and usage.get(key.removesuffix("_tokens")) != expected[key]:
                        raise RuntimeError("otlp_readback_usage_mismatch")
        print(
            json.dumps(
                {
                    "passed": True,
                    "trace_id": identity,
                    "span_count": len(local),
                    "exact_span_ids": True,
                    "parent_child_verified": True,
                    "generation_type_and_usage_verified": True,
                    "synthetic_model_usage_only": args.command == "check",
                    "url": base + "/project/qt-agent-hub/traces/" + identity,
                }
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            f"Observability setup/check failed: {type(error).__name__}. Credentials and response bodies withheld.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
