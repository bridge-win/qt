"""Static packaging contracts; these tests do not build or run Docker."""
from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_monitor_image_is_isolated_and_hash_enforced() -> None:
    source = read("Dockerfile.btc-monitor")
    assert source.count("--require-hashes") == 2
    assert "--only-binary=:all:" in source
    assert "--no-index" in source
    assert "COPY src/qt/__init__.py ./src/qt/__init__.py" in source
    assert "COPY src/qt/btc_monitor ./src/qt/btc_monitor" in source
    assert not re.search(r"^COPY src(?:\s|/\s)", source, re.MULTILINE)
    assert "pip install ." not in source
    assert "USER 10001:10001" in source
    assert 'ENTRYPOINT ["python", "-m", "qt.btc_monitor"]' in source
    assert 'CMD ["run", "--state-dir", "/state"]' in source
    for line in source.splitlines():
        if line.startswith("FROM "):
            assert re.match(r"FROM python:3\.12\.11-slim-bookworm@sha256:[a-f0-9]{64} AS ", line)


def test_monitor_compose_has_one_hardened_no_port_worker() -> None:
    compose = yaml.safe_load(read("docker-compose.btc-monitor.yml"))
    assert set(compose["services"]) == {"monitor"}
    service = compose["services"]["monitor"]
    assert "ports" not in service
    assert service["restart"] == "unless-stopped"
    assert service["read_only"] and service["init"]
    assert service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["command"] == ["run", "--state-dir", "/state"]
    assert service["environment"]["QT_BTC_MONITOR_STATE_DIR"] == "/state"
    assert service["healthcheck"]["test"] == [
        "CMD", "python", "-m", "qt.btc_monitor", "health", "--state-dir", "/state",
    ]
    assert service["mem_limit"] and service["cpus"] and service["pids_limit"]
    assert service["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
    assert not any("BINANCE" in key or "CODEX" in key for key in service["environment"])


def test_monitor_compose_reuses_existing_workbench_state() -> None:
    service = yaml.safe_load(read("docker-compose.btc-monitor.yml"))["services"]["monitor"]
    assert len(service["volumes"]) == 1
    volume = service["volumes"][0]
    assert volume["type"] == "bind"
    assert volume["source"] == "${QT_BTC_MONITOR_HOST_STATE_DIR:-./data/workbench/btc-monitor}"
    assert volume["target"] == "/state"
    assert volume["bind"]["create_host_path"] is False
    assert service["group_add"] == ["${QT_BTC_MONITOR_READ_GID:-10001}"]


def test_monitor_lock_contains_only_pinned_hashed_runtime_closure() -> None:
    source = read("requirements-btc-monitor.lock")
    entries = re.findall(r"^([a-z0-9-]+)==([^\s]+) \\\n((?:    --hash=sha256:[a-f0-9]{64}(?: \\)?\n)+)", source, re.MULTILINE)
    names = {name for name, _version, _hashes in entries}
    assert names == {
        "annotated-types", "anyio", "certifi", "h11", "httpcore", "httpx", "idna",
        "pydantic", "pydantic-core", "typing-extensions", "typing-inspection", "websockets",
    }
    assert "--constraint requirements-platform.lock" in source
    assert "--python-version 3.12" in source
    assert "--python-platform linux" in source
    assert {line for line in read("requirements-btc-monitor.in").splitlines() if line and not line.startswith("#")} == {
        "httpx==0.28.1", "pydantic==2.13.4", "websockets==16.1.1",
    }


def test_monitor_ci_builds_actual_image_without_publication_or_credentials() -> None:
    job = yaml.safe_load(read(".github/workflows/ci.yml"))["jobs"]["btc-monitor-image"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["if"] == "github.event_name != 'schedule'"
    assert job["timeout-minutes"] == 15
    assert not {"permissions", "secrets", "environment"} & set(job)
    steps = job["steps"]
    assert steps[0]["uses"] == "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
    assert steps[1]["uses"] == "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c"
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "config --quiet" in commands
    assert "--env-file config/btc-monitor.env.example" in commands
    assert "docker buildx build --load --platform linux/amd64 --file Dockerfile.btc-monitor" in commands
    assert "--target runtime --tag qt-btc-monitor:ci" in commands
    assert "bash scripts/ci_btc_monitor_image_smoke.sh qt-btc-monitor:ci" in commands
    assert "--push" not in commands and "secrets." not in commands


def test_monitor_ci_runtime_smoke_checks_hardening_and_lifecycle() -> None:
    source = read("scripts/ci_btc_monitor_image_smoke.sh")
    assert "hardening=(--network none --init --read-only --user 10001:10001" in source
    assert "--cap-drop ALL --security-opt no-new-privileges:true" in source
    assert "--tmpfs /tmp:rw,size=32m,mode=1777 --memory 384m --cpus 1 --pids-limit 64" in source
    assert 'source=$fixture,target=/smoke/worker.py,readonly' in source
    assert "--entrypoint python" in source
    assert "wait_health unhealthy" in source and "wait_health healthy" in source
    assert "--restart unless-stopped" in source and "{{.RestartCount}}" in source
    assert "signal.SIGSTOP" in source and "signal.SIGCONT" in source
    assert 'docker stop --time 20 "$container"' in source
    assert 'snapshot["health"]["alive"] is False' in source
    assert "stat.S_IMODE(info.st_mode)==0o640" in source
    assert "--user \"20002:$read_gid\"" in source
    assert "curl " not in source and "wget " not in source


def test_monitor_ci_fixture_emits_only_offline_market_events() -> None:
    spec = importlib.util.spec_from_file_location(
        "btc_monitor_image_fixture", ROOT / "tests/fixtures/btc_monitor_image_worker.py"
    )
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    events = []

    async def collect() -> None:
        stop = asyncio.Event()

        def emit(event) -> None:
            events.append(event)
            if event.channel == "trade":
                stop.set()

        await fixture.fixture_feed(None, emit, lambda reason: None, stop, lambda reason: None)

    asyncio.run(collect())
    assert [event.channel for event in events] == ["candle", "candle", "candle", "quote", "trade"]
    assert all(event.historical for event in events[:3])
    assert events[1].sequence - events[0].sequence == 60000
    assert events[-2].event_time is None and events[-1].event_time is not None
