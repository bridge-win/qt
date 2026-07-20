from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event

from qt.platform.api import create_app
from qt.platform.commands import CommandRepository
from qt.platform.config import PlatformSettings
from qt.platform.database import SessionFactory, create_platform_engine, create_session_factory
from qt.platform.health import ExpectedWorker
from qt.platform.models import Base
from qt.platform.operations import OperationsRepository
from qt.platform.schemas import CommandType, WorkerStatus


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc))


@pytest.fixture
def settings(tmp_path: Path) -> PlatformSettings:
    return PlatformSettings(
        platform_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'api.db'}",
        worker_stale_seconds=60,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def engine(settings: PlatformSettings) -> Iterator[Engine]:
    database_engine = create_platform_engine(settings)
    Base.metadata.create_all(database_engine)
    yield database_engine
    Base.metadata.drop_all(database_engine)
    database_engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> SessionFactory:
    return create_session_factory(engine)


def test_liveness_has_no_dependency_side_effects(settings: PlatformSettings) -> None:
    calls = 0

    def fail_if_called() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("liveness touched persistence")

    app = create_app(
        settings=settings,
        session_factory=cast(SessionFactory, fail_if_called),
    )

    response = TestClient(app).get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert calls == 0


def test_app_owned_engine_is_disposed_once_without_liveness_connecting(
    settings: PlatformSettings,
) -> None:
    app = create_app(settings=settings)
    owned_engine = cast(Engine, app.state.platform_engine)
    connections = 0
    disposals = 0

    def record_connection(_dbapi_connection: object, _connection_record: object) -> None:
        nonlocal connections
        connections += 1

    def record_disposal(_engine: Engine) -> None:
        nonlocal disposals
        disposals += 1

    event.listen(owned_engine, "connect", record_connection)
    event.listen(owned_engine, "engine_disposed", record_disposal)
    try:
        with TestClient(app) as client:
            response = client.get("/api/health/live")
        observed_connections = connections
        observed_disposals = disposals
    finally:
        event.remove(owned_engine, "connect", record_connection)
        event.remove(owned_engine, "engine_disposed", record_disposal)
        if disposals == 0:
            owned_engine.dispose()

    assert response.json() == {"status": "alive"}
    assert observed_connections == 0
    assert observed_disposals == 1


def test_externally_owned_engine_is_not_disposed(
    settings: PlatformSettings,
    engine: Engine,
) -> None:
    disposals = 0

    def record_disposal(_engine: Engine) -> None:
        nonlocal disposals
        disposals += 1

    event.listen(engine, "engine_disposed", record_disposal)
    try:
        app = create_app(settings=settings, session_factory=create_session_factory(engine))
        with TestClient(app) as client:
            assert client.get("/api/health/live").status_code == 200
        observed_disposals = disposals
    finally:
        event.remove(engine, "engine_disposed", record_disposal)

    assert app.state.platform_engine is None
    assert observed_disposals == 0


def test_readiness_returns_503_when_database_is_unavailable(
    settings: PlatformSettings,
    engine: Engine,
) -> None:
    def reject_query(
        _connection: object,
        _cursor: object,
        _statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        raise RuntimeError("private database failure")

    event.listen(engine, "before_cursor_execute", reject_query)
    app = create_app(settings=settings, session_factory=create_session_factory(engine))

    try:
        response = TestClient(app, raise_server_exceptions=False).get("/api/health/ready")
    finally:
        event.remove(engine, "before_cursor_execute", reject_query)

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "status": "not_ready",
            "dependencies": {"database": {"status": "unavailable"}},
        }
    }
    assert "private database failure" not in response.text


def test_readiness_returns_200_when_database_is_available(
    settings: PlatformSettings,
    session_factory: SessionFactory,
) -> None:
    app = create_app(settings=settings, session_factory=session_factory)

    response = TestClient(app).get("/api/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"database": {"status": "healthy"}},
    }


def test_worker_health_endpoint_returns_expected_workers_in_stable_order(
    settings: PlatformSettings,
    session_factory: SessionFactory,
    clock: MutableClock,
) -> None:
    operations = OperationsRepository(session_factory, clock=clock)
    operations.record_heartbeat(
        role="trading",
        instance_id="worker-a",
        status=WorkerStatus.HEALTHY,
        version="1.0.0",
        details={"mode": "paper"},
    )
    clock.current += timedelta(seconds=61)
    app = create_app(
        settings=settings,
        session_factory=session_factory,
        expected_workers=(
            ExpectedWorker(role="trading", instance_id="worker-a"),
            ExpectedWorker(role="job", instance_id="worker-a"),
        ),
        clock=clock,
    )

    response = TestClient(app).get("/api/health/workers")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "workers": [
            {
                "role": "job",
                "instance_id": "worker-a",
                "status": "missing",
                "reported_status": None,
                "version": None,
                "last_seen_at": None,
            },
            {
                "role": "trading",
                "instance_id": "worker-a",
                "status": "stale",
                "reported_status": "healthy",
                "version": "1.0.0",
                "last_seen_at": "2026-07-21T08:00:00Z",
            },
        ],
    }


def test_commands_endpoint_returns_detached_recent_views(
    settings: PlatformSettings,
    session_factory: SessionFactory,
    clock: MutableClock,
) -> None:
    commands = CommandRepository(session_factory, clock=clock)
    first = commands.enqueue(
        owner_id="operator-a",
        command_type=CommandType.NOOP,
        target="trading",
        payload={"sequence": 1},
        idempotency_key="noop-1",
    )
    clock.current += timedelta(seconds=1)
    second = commands.enqueue(
        owner_id="operator-a",
        command_type=CommandType.RECONCILE,
        target="portfolio",
        payload={"sequence": 2},
        idempotency_key="reconcile-1",
    )
    commands.enqueue(
        owner_id="operator-b",
        command_type=CommandType.NOOP,
        target="job",
        payload={},
        idempotency_key="noop-2",
    )
    app = create_app(settings=settings, session_factory=session_factory, clock=clock)

    response = TestClient(app).get("/api/v1/commands?owner_id=operator-a&limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload] == [str(second.id), str(first.id)]
    assert payload[0]["payload"] == {"sequence": 2}
    assert payload[0]["status"] == "pending"
    assert app.state.command_repository is not commands


def test_openapi_contract_is_valid_and_read_only(
    settings: PlatformSettings,
    session_factory: SessionFactory,
) -> None:
    app = create_app(settings=settings, session_factory=session_factory)

    schema = app.openapi()

    assert schema["info"] == {"title": "QT Control API", "version": "1.0.0"}
    assert set(schema["paths"]) == {
        "/api/health/live",
        "/api/health/ready",
        "/api/health/workers",
        "/api/v1/commands",
    }
    assert all(set(operations) <= {"get", "parameters"} for operations in schema["paths"].values())
    assert schema["components"]["schemas"]["LivenessResponse"]["properties"]["status"] == {
        "const": "alive",
        "title": "Status",
        "type": "string",
    }
    assert app.state.command_repository is not None
    assert app.state.health_service is not None


def test_platform_api_entrypoint_is_typed_and_strategy_free() -> None:
    script_path = Path(__file__).parents[2] / "scripts" / "run_platform_api.py"
    source = script_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert "qt.strategies" not in source
    assert not any(module.startswith("qt.strategies") for module in imports)
    assert "uvicorn" in imports
    main = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    assert main.returns is not None
    uvicorn_call = next(
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    )
    assert {keyword.arg for keyword in uvicorn_call.keywords} == {"host", "port"}
    help_result = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "--host HOST" in help_result.stdout
    assert "--port PORT" in help_result.stdout
    assert "--expected-worker ROLE:INSTANCE" in help_result.stdout


def test_platform_api_entrypoint_wires_expected_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_platform_api_script()
    settings_marker = object()
    app_marker = FastAPI()
    captured: dict[str, object] = {}

    def fake_settings() -> object:
        return settings_marker

    def fake_create_app(
        *,
        settings: object,
        expected_workers: Sequence[ExpectedWorker],
    ) -> FastAPI:
        captured["settings"] = settings
        captured["expected_workers"] = tuple(expected_workers)
        return app_marker

    def fake_run(app: object, *, host: str, port: int) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(module, "PlatformSettings", fake_settings)
    monkeypatch.setattr(module, "create_app", fake_create_app)
    monkeypatch.setattr(module.uvicorn, "run", fake_run)
    main = cast(Callable[[Sequence[str] | None], None], module.main)

    main(
        (
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--expected-worker",
            "trading:trading-a",
            "--expected-worker",
            "job:job-a",
        )
    )

    assert captured == {
        "settings": settings_marker,
        "expected_workers": (
            ExpectedWorker(role="trading", instance_id="trading-a"),
            ExpectedWorker(role="job", instance_id="job-a"),
        ),
        "app": app_marker,
        "host": "0.0.0.0",
        "port": 9000,
    }


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--expected-worker", "trading"), "ROLE:INSTANCE"),
        (("--expected-worker", "trading:worker:extra"), "exactly one ':'"),
        (("--expected-worker", ":worker-a"), "non-empty role and instance"),
        (("--expected-worker", "trading:"), "non-empty role and instance"),
        (
            (
                "--expected-worker",
                "trading:worker-a",
                "--expected-worker",
                "trading:worker-a",
            ),
            "duplicate expected worker",
        ),
    ],
)
def test_platform_api_entrypoint_rejects_invalid_expected_workers(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    script_path = Path(__file__).parents[2] / "scripts" / "run_platform_api.py"

    result = subprocess.run(
        [sys.executable, str(script_path), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert message in result.stderr


def _load_platform_api_script() -> ModuleType:
    script_path = Path(__file__).parents[2] / "scripts" / "run_platform_api.py"
    spec = importlib.util.spec_from_file_location("qt_task5_run_platform_api", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load platform API entrypoint")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
