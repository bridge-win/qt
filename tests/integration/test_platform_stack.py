from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = PROJECT_ROOT / "docker-compose.platform.yml"
DOCKERFILE_PATH = PROJECT_ROOT / "Dockerfile.platform"
ENTRYPOINT_PATH = PROJECT_ROOT / "deploy" / "platform-entrypoint.sh"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.platform.example"
DOCKERIGNORE_PATH = PROJECT_ROOT / ".dockerignore"
LOCK_PATH = PROJECT_ROOT / "requirements-platform.lock"
CI_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
OPERATIONS_PATH = PROJECT_ROOT / "docs" / "operations.md"


def _yaml(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _services() -> dict[str, dict[str, object]]:
    compose = _yaml(COMPOSE_PATH)
    services = compose["services"]
    assert isinstance(services, dict)
    return cast(dict[str, dict[str, object]], services)


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

    assert len(re.findall(r"^FROM python:3\.12[^\n]+", dockerfile, re.MULTILINE)) == 2
    assert " AS builder" in dockerfile
    assert "pip wheel" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--no-index" in dockerfile
    assert "--no-cache-dir" in dockerfile
    assert "--no-compile" in dockerfile
    assert "USER qt" in dockerfile
    assert "WORKDIR /app" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/platform-entrypoint"]' in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    runtime = dockerfile.split("FROM ", maxsplit=2)[-1]
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
    assert {".env", ".env.*", ".git", ".superpowers", "data", "tests"} <= dockerignore


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

    assert "QT_PLATFORM_ENV=production" in example
    assert "POSTGRES_PASSWORD=__GENERATE_ME__" in example
    assert "QT_DATABASE_URL=postgresql+psycopg://" in example
    assert "__GENERATE_ME__" in example
    assert "secrets.token_urlsafe" in example
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
    assert cast(dict[str, object], integration["services"])["postgres"]
    assert integration["runs-on"] == "ubuntu-latest"
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
