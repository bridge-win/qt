from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import cast

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = PROJECT_ROOT / "docker-compose.platform.yml"
LOCAL_COMPOSE_PATH = PROJECT_ROOT / "docker-compose.platform.local.yml"
DOCKERFILE_PATH = PROJECT_ROOT / "Dockerfile.platform"
ENTRYPOINT_PATH = PROJECT_ROOT / "deploy" / "platform-entrypoint.sh"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.platform.example"
DOCKERIGNORE_PATH = PROJECT_ROOT / ".dockerignore"
LOCK_PATH = PROJECT_ROOT / "requirements-platform.lock"
CI_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
OPERATIONS_PATH = PROJECT_ROOT / "docs" / "operations.md"
PYTHON_IMAGE = (
    "python:3.12.11-slim-bookworm@"
    "sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
)
POSTGRES_IMAGE = (
    "postgres:17.9-bookworm@"
    "sha256:47f917f7409eacd22fc5dfb1dee634e1b55cf0c01d1a7eb701be2227a03e0641"
)


def _yaml(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _services() -> dict[str, dict[str, object]]:
    compose = _yaml(COMPOSE_PATH)
    services = compose["services"]
    assert isinstance(services, dict)
    return cast(dict[str, dict[str, object]], services)


def _dockerfile_instructions() -> list[tuple[str, str]]:
    logical_lines: list[str] = []
    current = ""
    for raw_line in DOCKERFILE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current = f"{current} {stripped}".strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        logical_lines.append(current)
        current = ""
    assert not current
    return [
        (instruction.upper(), arguments)
        for instruction, arguments in (line.split(maxsplit=1) for line in logical_lines)
    ]


def _compose_config(*files: Path, image: str) -> dict[str, object]:
    environment = os.environ.copy()
    environment["QT_PLATFORM_IMAGE"] = image
    command = ["docker", "compose", "--env-file", str(ENV_EXAMPLE_PATH)]
    for path in files:
        command.extend(("-f", str(path)))
    command.append("config")
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = yaml.safe_load(result.stdout)
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def test_compose_separates_roles_and_orders_migrations() -> None:
    services = _services()

    assert set(services) == {"postgres", "migrate", "api", "trading-worker"}
    assert services["migrate"]["restart"] == "no"
    assert services["api"]["command"] != services["trading-worker"]["command"]
    assert services["migrate"]["depends_on"] == {
        "postgres": {"condition": "service_healthy"}
    }
    for service_name in ("api", "trading-worker"):
        assert services[service_name]["depends_on"] == {
            "migrate": {"condition": "service_completed_successfully"}
        }


def test_compose_uses_configurable_immutable_image_and_explicit_local_build() -> None:
    services = _services()
    image_reference = "${QT_PLATFORM_IMAGE:?set QT_PLATFORM_IMAGE}"

    for service_name in ("migrate", "api", "trading-worker"):
        assert services[service_name]["image"] == image_reference
        assert "build" not in services[service_name]
    assert "qt-platform:" not in COMPOSE_PATH.read_text(encoding="utf-8")

    assert LOCAL_COMPOSE_PATH.is_file()
    local_services = cast(dict[str, dict[str, object]], _yaml(LOCAL_COMPOSE_PATH)["services"])
    for service_name in ("migrate", "api", "trading-worker"):
        assert local_services[service_name]["image"] == (
            "${QT_PLATFORM_LOCAL_IMAGE:-qt-platform:local}"
        )
        assert local_services[service_name]["pull_policy"] == "never"
        assert local_services[service_name]["build"] == {
            "context": ".",
            "dockerfile": "Dockerfile.platform",
        }


def test_compose_resolves_registry_digest_and_local_build_references() -> None:
    digest_image = (
        "registry.example.invalid/qt/platform@"
        "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    )
    production = _compose_config(COMPOSE_PATH, image=digest_image)
    production_services = cast(dict[str, dict[str, object]], production["services"])
    for service_name in ("migrate", "api", "trading-worker"):
        assert production_services[service_name]["image"] == digest_image

    local = _compose_config(
        COMPOSE_PATH,
        LOCAL_COMPOSE_PATH,
        image="qt-platform:ignored-by-local-override",
    )
    local_services = cast(dict[str, dict[str, object]], local["services"])
    for service_name in ("migrate", "api", "trading-worker"):
        assert local_services[service_name]["image"] == "qt-platform:local"
        assert local_services[service_name]["pull_policy"] == "never"


def test_compose_persists_only_postgresql_and_isolates_backend() -> None:
    compose = _yaml(COMPOSE_PATH)
    services = _services()

    assert set(cast(dict[str, object], compose["volumes"])) == {"postgres-data"}
    assert services["postgres"]["volumes"] == ["postgres-data:/var/lib/postgresql/data"]
    for service_name in ("migrate", "api", "trading-worker"):
        assert "volumes" not in services[service_name]
    networks = cast(dict[str, dict[str, object]], compose["networks"])
    assert networks["backend"]["internal"] is True
    assert services["postgres"]["networks"] == ["backend"]
    assert services["trading-worker"]["networks"] == ["backend"]
    assert set(cast(list[str], services["api"]["networks"])) == {"backend", "edge"}
    assert services["postgres"]["image"] == POSTGRES_IMAGE


def test_compose_hardens_every_application_container() -> None:
    services = _services()

    for service_name in ("migrate", "api", "trading-worker"):
        service = services[service_name]
        assert service["read_only"] is True
        assert service["init"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in cast(list[str], service["security_opt"])
        assert cast(list[str], service["tmpfs"])
    assert services["postgres"]["restart"] == "unless-stopped"
    assert services["api"]["restart"] == "unless-stopped"
    assert services["trading-worker"]["restart"] == "unless-stopped"


def test_compose_healthchecks_use_typed_platform_probes() -> None:
    services = _services()

    postgres_probe = cast(dict[str, object], services["postgres"]["healthcheck"])["test"]
    assert "pg_isready" in " ".join(cast(list[str], postgres_probe))

    api_probe = cast(dict[str, object], services["api"]["healthcheck"])["test"]
    assert cast(list[str], api_probe)[:4] == ["CMD", "python", "-m", "qt.platform.probe"]
    assert "api" in cast(list[str], api_probe)

    worker_probe = cast(
        dict[str, object], services["trading-worker"]["healthcheck"]
    )["test"]
    assert cast(list[str], worker_probe)[:4] == [
        "CMD",
        "python",
        "-m",
        "qt.platform.probe",
    ]
    assert "worker" in cast(list[str], worker_probe)
    assert "--role" in cast(list[str], worker_probe)
    assert "--instance-id" in cast(list[str], worker_probe)


def test_api_and_worker_use_the_same_expected_identity() -> None:
    services = _services()
    api_command = cast(list[str], services["api"]["command"])
    worker_command = cast(list[str], services["trading-worker"]["command"])

    assert api_command == [
        "api",
        "--host",
        "0.0.0.0",
        "--port",
        "8876",
        "--expected-worker",
        "trading:${QT_TRADING_WORKER_ID:?set QT_TRADING_WORKER_ID}",
    ]
    assert worker_command == [
        "trading-worker",
        "--worker-id",
        "${QT_TRADING_WORKER_ID:?set QT_TRADING_WORKER_ID}",
        "--poll-seconds",
        "${QT_TRADING_WORKER_POLL_SECONDS:-1}",
    ]


def test_compose_requires_database_secrets_without_real_defaults() -> None:
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    services = _services()

    assert "${POSTGRES_PASSWORD:?" in compose_text
    assert "${QT_DATABASE_URL:?" in compose_text
    assert "postgresql+psycopg://qt:qt@" not in compose_text
    assert "POSTGRES_PASSWORD: postgres" not in compose_text
    for service_name in ("migrate", "api", "trading-worker"):
        environment = cast(dict[str, str], services[service_name]["environment"])
        assert environment["QT_PLATFORM_ENV"] == "${QT_PLATFORM_ENV:?set QT_PLATFORM_ENV}"
        assert environment["QT_DATABASE_URL"] == "${QT_DATABASE_URL:?set QT_DATABASE_URL}"


def test_platform_image_is_multistage_wheel_only_and_non_root() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    instructions = _dockerfile_instructions()
    from_instructions = [arguments for instruction, arguments in instructions if instruction == "FROM"]
    run_instructions = [arguments for instruction, arguments in instructions if instruction == "RUN"]

    assert dockerfile.startswith(
        "# syntax=docker/dockerfile:1.7@"
        "sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
    )
    assert from_instructions == [
        f"{PYTHON_IMAGE} AS wheelhouse",
        f"{PYTHON_IMAGE} AS builder",
        f"{PYTHON_IMAGE} AS runtime",
    ]
    assert any("pip download" in run and "--require-hashes" in run for run in run_instructions)
    offline_runs = [run for run in run_instructions if run.startswith("--network=none ")]
    assert len(offline_runs) >= 3
    assert any(
        "pip wheel" in run and "--no-build-isolation" in run and "--no-index" in run
        for run in offline_runs
    )
    assert any("pip install" in run and "setuptools==" in run and "wheel==" in run for run in offline_runs)
    assert "--no-cache-dir" in dockerfile
    assert "--no-compile" in dockerfile
    assert "USER qt" in dockerfile
    assert "WORKDIR /app" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/platform-entrypoint"]' in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    runtime = dockerfile.rsplit("\nFROM ", maxsplit=1)[-1]
    assert "build-essential" not in runtime
    assert "gcc" not in runtime
    assert "COPY --from=builder" in runtime


def test_platform_dependencies_are_fully_pinned_and_build_context_is_clean() -> None:
    lock = LOCK_PATH.read_text(encoding="utf-8")
    dockerignore = set(DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines())

    requirement_lines = [
        line for line in lock.splitlines() if line and not line[0].isspace() and line[0] != "#"
    ]
    assert requirement_lines
    assert all("==" in line for line in requirement_lines)
    assert "--hash=sha256:" in lock
    assert re.search(r"^setuptools==[^\s]+ \\$", lock, re.MULTILINE)
    assert re.search(r"^wheel==[^\s]+ \\$", lock, re.MULTILINE)
    assert {".env", ".env.*", ".git", ".superpowers", "data", "tests"} <= dockerignore


def test_backup_artifacts_are_excluded_from_git_and_build_context() -> None:
    candidates = (
        "backups/review.dump",
        "review.dump",
        "review.dump.sha256",
        "review.dump.tmp",
        "review.dump.sha256.tmp",
    )
    for candidate in candidates:
        subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", candidate],
            cwd=PROJECT_ROOT,
            check=True,
        )

    dockerignore = set(DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines())
    assert {
        "backups",
        "*.dump",
        "*.dump.sha256",
        "*.dump.tmp",
        "*.dump.sha256.tmp",
    } <= dockerignore


def test_entrypoint_allowlists_roles_and_execs_without_eval() -> None:
    entrypoint = ENTRYPOINT_PATH.read_text(encoding="utf-8")

    assert entrypoint.startswith("#!/bin/sh\nset -eu\n")
    for role in ("migrate", "api", "trading-worker"):
        assert role in entrypoint
    assert entrypoint.count("exec ") >= 3
    assert "eval " not in entrypoint
    assert "Unknown platform role" in entrypoint


def test_environment_example_is_safe_and_reproducible() -> None:
    example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    assert "QT_PLATFORM_ENV=staging" in example
    assert "POSTGRES_PASSWORD=__GENERATE_ME__" in example
    assert "QT_DATABASE_URL=postgresql+psycopg://" in example
    assert "__GENERATE_ME__" in example
    assert "secrets.token_urlsafe" in example
    assert "deploy/create_platform_env.py" in example
    assert "Path.write_text" not in example
    assert "QT_PLATFORM_IMAGE=qt-platform:local" in example
    assert "docker compose --env-file .env.platform" in example
    assert "qt:qt" not in example


def test_ci_adds_postgresql_integration_without_weakening_existing_matrix() -> None:
    workflow = _yaml(CI_PATH)
    jobs = cast(dict[str, dict[str, object]], workflow["jobs"])

    assert "test" in jobs
    matrix = cast(
        dict[str, object], cast(dict[str, object], jobs["test"]["strategy"])["matrix"]
    )
    assert matrix["python-version"] == ["3.10", "3.11", "3.12"]
    integration = jobs["platform-postgresql"]
    postgres_service = cast(
        dict[str, dict[str, object]], integration["services"]
    )["postgres"]
    assert postgres_service["image"] == POSTGRES_IMAGE
    assert integration["runs-on"] == "ubuntu-latest"
    assert "platform-image" in jobs
    image_job = jobs["platform-image"]
    image_steps = cast(list[dict[str, object]], image_job["steps"])
    image_commands = "\n".join(
        str(step.get("run", "")) for step in image_steps
    )
    assert "docker compose" in image_commands and "config --quiet" in image_commands
    assert "docker buildx build" in image_commands
    assert "linux/amd64,linux/arm64" in image_commands

    for job in jobs.values():
        for step in cast(list[dict[str, object]], job["steps"]):
            uses = step.get("uses")
            if uses is not None:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", str(uses))
    text = CI_PATH.read_text(encoding="utf-8")
    assert "python-version: \"3.12\"" in text
    assert "QT_TEST_POSTGRES_URL" in text
    assert "pytest tests/integration -q" in text


def test_operations_runbook_covers_platform_lifecycle_and_recovery() -> None:
    operations = OPERATIONS_PATH.read_text(encoding="utf-8")
    required_phrases = (
        "docker compose --env-file .env.platform",
        "config --quiet",
        "build",
        "up -d",
        "alembic current",
        "/api/health/live",
        "/api/health/ready",
        "/api/health/workers",
        "/api/v1/commands",
        "logs",
        "stop",
        "pg_dump",
        "pg_restore",
        "rollback",
        "clean volume",
        "secret rotation",
        "run_all.py",
        "deploy/create_platform_env.py",
        "deploy/platform-backup.sh",
        "docker-compose.platform.local.yml",
        "docker buildx imagetools inspect",
        "immutable digest",
        "previously recorded digest",
        "sha256sum --check",
    )
    for phrase in required_phrases:
        assert phrase.lower() in operations.lower()


def test_api_process_remains_free_of_strategy_startup_paths() -> None:
    api_source = (PROJECT_ROOT / "src" / "qt" / "platform" / "api.py").read_text(
        encoding="utf-8"
    )
    api_cli = (PROJECT_ROOT / "scripts" / "run_platform_api.py").read_text(
        encoding="utf-8"
    )

    for forbidden in ("qt.strategies", "run_all", "run_strategy_forever"):
        assert forbidden not in api_source
        assert forbidden not in api_cli
