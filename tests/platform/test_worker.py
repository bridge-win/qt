from __future__ import annotations

import ast
import importlib.util
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType, ModuleType
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import Engine

from qt.platform.commands import CommandRepository
from qt.platform.config import PlatformSettings
from qt.platform.database import SessionFactory, create_platform_engine, create_session_factory
from qt.platform.models import Base
from qt.platform.operations import OperationsRepository
from qt.platform.schemas import (
    CommandStatus,
    CommandType,
    CommandView,
    WorkerHeartbeatView,
    WorkerStatus,
)
from qt.platform.worker import TradingWorker


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class RecordingStopEvent:
    def __init__(self, *, stop_on_wait: bool = False) -> None:
        self.stopped = False
        self.stop_on_wait = stop_on_wait
        self.waits: list[float] = []
        self.set_calls = 0

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.set_calls += 1
        self.stopped = True

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if self.stop_on_wait:
            self.stopped = True
        return self.stopped


@dataclass(frozen=True)
class HeartbeatCall:
    role: str
    instance_id: str
    status: WorkerStatus
    version: str
    details: dict[str, object]


class RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[HeartbeatCall] = []

    def record_heartbeat(
        self,
        *,
        role: str,
        instance_id: str,
        status: WorkerStatus,
        version: str,
        details: Mapping[str, object],
    ) -> WorkerHeartbeatView:
        frozen_details = dict(details)
        self.calls.append(
            HeartbeatCall(
                role=role,
                instance_id=instance_id,
                status=status,
                version=version,
                details=frozen_details,
            )
        )
        return cast(WorkerHeartbeatView, object())


class ExplodingCommands:
    def claim_next(self, *, worker_id: str, lease_seconds: int) -> CommandView | None:
        raise RuntimeError(f"database unavailable for {worker_id}:{lease_seconds}")

    def complete(
        self,
        *,
        command_id: UUID,
        claim_token: UUID,
        result: Mapping[str, object],
    ) -> CommandView:
        raise AssertionError("complete must not be called")

    def fail(
        self,
        *,
        command_id: UUID,
        claim_token: UUID,
        error: str,
        retry_delay_seconds: int | None,
    ) -> CommandView:
        raise AssertionError("fail must not be called")


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc))


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    settings = PlatformSettings(
        platform_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'worker.db'}",
        database_echo=False,
        command_lease_seconds=30,
        worker_stale_seconds=60,
        _env_file=None,  # type: ignore[call-arg]
    )
    database_engine = create_platform_engine(settings)
    Base.metadata.create_all(database_engine)
    yield database_engine
    Base.metadata.drop_all(database_engine)
    database_engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> SessionFactory:
    return create_session_factory(engine)


@pytest.fixture
def commands(session_factory: SessionFactory, clock: MutableClock) -> CommandRepository:
    return CommandRepository(session_factory, clock=clock)


@pytest.fixture
def operations(session_factory: SessionFactory, clock: MutableClock) -> OperationsRepository:
    return OperationsRepository(session_factory, clock=clock)


@pytest.fixture
def worker(commands: CommandRepository, operations: OperationsRepository) -> TradingWorker:
    return TradingWorker(
        commands,
        operations,
        worker_id="worker-a",
        version="1.0.0",
        command_lease_seconds=30,
        poll_seconds=1.0,
    )


def enqueue(
    commands: CommandRepository,
    *,
    command_type: CommandType = CommandType.NOOP,
    key: str = "worker-command-1",
    max_attempts: int = 3,
) -> CommandView:
    return commands.enqueue(
        owner_id="operator",
        command_type=command_type,
        target="platform",
        payload={},
        idempotency_key=key,
        max_attempts=max_attempts,
    )


def test_worker_completes_one_claimed_noop_with_deterministic_result(
    worker: TradingWorker,
    commands: CommandRepository,
) -> None:
    first = enqueue(commands, key="noop-1")
    second = enqueue(commands, key="noop-2")

    assert worker.run_once() is True

    completed = commands.get(first.id)
    waiting = commands.get(second.id)
    assert completed is not None
    assert completed.status is CommandStatus.SUCCEEDED
    assert completed.result == {"handled": True}
    assert waiting is not None
    assert waiting.status is CommandStatus.PENDING


def test_worker_records_healthy_heartbeat_when_idle(
    worker: TradingWorker,
    operations: OperationsRepository,
) -> None:
    assert worker.run_once() is False

    heartbeats = operations.list_heartbeats(role="trading")
    assert len(heartbeats) == 1
    assert heartbeats[0].instance_id == "worker-a"
    assert heartbeats[0].status is WorkerStatus.HEALTHY
    assert heartbeats[0].details == {"state": "idle"}


@pytest.mark.parametrize(
    "command_type",
    [CommandType.START, CommandType.STOP, CommandType.RESTART, CommandType.RECONCILE],
)
def test_worker_terminally_fails_phase_two_lifecycle_commands(
    worker: TradingWorker,
    commands: CommandRepository,
    command_type: CommandType,
) -> None:
    queued = enqueue(commands, command_type=command_type)

    assert worker.run_once() is True

    failed = commands.get(queued.id)
    assert failed is not None
    assert failed.status is CommandStatus.FAILED
    assert failed.attempts == 1
    assert failed.error == f"Phase 2 lifecycle command '{command_type.value}' is not enabled"
    assert failed.claim_token is None


def test_handler_failures_use_attempt_based_bounded_retry_without_waiting(
    commands: CommandRepository,
    operations: OperationsRepository,
    clock: MutableClock,
) -> None:
    def fail_handler(_command: CommandView) -> Mapping[str, object]:
        raise RuntimeError("temporary outage")

    stop_event = RecordingStopEvent()
    worker = TradingWorker(
        commands,
        operations,
        worker_id="worker-a",
        version="1.0.0",
        command_lease_seconds=30,
        poll_seconds=1.0,
        retry_base_seconds=2,
        retry_max_seconds=5,
        handlers={CommandType.NOOP: fail_handler},
        stop_event=stop_event,
    )
    queued = enqueue(commands, max_attempts=4)

    expected_delays = (2, 4, 5)
    for attempt, delay in enumerate(expected_delays, start=1):
        assert worker.run_once() is True
        current = commands.get(queued.id)
        assert current is not None
        assert current.status is CommandStatus.RETRY_WAIT
        assert current.attempts == attempt
        assert current.available_at == clock.current + timedelta(seconds=delay)
        assert current.error == "RuntimeError: temporary outage"
        clock.advance(seconds=delay)

    assert worker.run_once() is True
    terminal = commands.get(queued.id)
    assert terminal is not None
    assert terminal.status is CommandStatus.FAILED
    assert terminal.attempts == 4
    assert stop_event.waits == []


def test_completion_claim_theft_does_not_overwrite_the_new_owner(
    commands: CommandRepository,
    operations: OperationsRepository,
    clock: MutableClock,
) -> None:
    stolen_token: UUID | None = None

    def steal_claim(_command: CommandView) -> Mapping[str, object]:
        nonlocal stolen_token
        clock.advance(seconds=6)
        stolen = commands.claim_next(worker_id="worker-b", lease_seconds=30)
        assert stolen is not None
        stolen_token = stolen.claim_token
        return {"handled": "too-late"}

    worker = TradingWorker(
        commands,
        operations,
        worker_id="worker-a",
        version="1.0.0",
        command_lease_seconds=5,
        poll_seconds=1.0,
        handlers={CommandType.NOOP: steal_claim},
    )
    queued = enqueue(commands)

    assert worker.run_once() is True

    current = commands.get(queued.id)
    assert current is not None
    assert current.status is CommandStatus.PROCESSING
    assert current.claim_owner == "worker-b"
    assert current.claim_token == stolen_token
    heartbeat = operations.list_heartbeats(role="trading")[0]
    assert heartbeat.status is WorkerStatus.DEGRADED
    assert heartbeat.details["state"] == "claim_lost"
    assert heartbeat.details["command_id"] == str(queued.id)


def test_failure_after_lease_expiry_is_not_reported_as_a_persisted_retry(
    commands: CommandRepository,
    operations: OperationsRepository,
    clock: MutableClock,
) -> None:
    def expire_then_fail(_command: CommandView) -> Mapping[str, object]:
        clock.advance(seconds=6)
        raise RuntimeError("late failure")

    worker = TradingWorker(
        commands,
        operations,
        worker_id="worker-a",
        version="1.0.0",
        command_lease_seconds=5,
        poll_seconds=1.0,
        handlers={CommandType.NOOP: expire_then_fail},
    )
    queued = enqueue(commands)

    assert worker.run_once() is True

    current = commands.get(queued.id)
    assert current is not None
    assert current.status is CommandStatus.PROCESSING
    assert current.claim_owner == "worker-a"
    heartbeat = operations.list_heartbeats(role="trading")[0]
    assert heartbeat.status is WorkerStatus.DEGRADED
    assert heartbeat.details["state"] == "claim_lost"


def test_run_forever_waits_after_loop_error_and_records_graceful_shutdown() -> None:
    operations = RecordingOperations()
    stop_event = RecordingStopEvent(stop_on_wait=True)
    worker = TradingWorker(
        ExplodingCommands(),
        operations,
        worker_id="worker-a",
        version="1.0.0",
        command_lease_seconds=30,
        poll_seconds=2.5,
        stop_event=stop_event,
    )

    worker.run_forever()

    assert stop_event.waits == [2.5]
    assert [call.status for call in operations.calls] == [
        WorkerStatus.STARTING,
        WorkerStatus.HEALTHY,
        WorkerStatus.DEGRADED,
        WorkerStatus.STOPPING,
        WorkerStatus.STOPPED,
    ]
    assert operations.calls[2].details == {
        "state": "loop_error",
        "error": "RuntimeError: database unavailable for worker-a:30",
    }
    assert operations.calls[-1].details == {"state": "stopped"}


def test_stop_is_idempotent() -> None:
    stop_event = RecordingStopEvent()
    worker = TradingWorker(
        ExplodingCommands(),
        RecordingOperations(),
        worker_id="worker-a",
        version="1.0.0",
        command_lease_seconds=30,
        poll_seconds=1.0,
        stop_event=stop_event,
    )

    worker.stop()
    worker.stop()

    assert stop_event.is_set()
    assert stop_event.set_calls == 1


@pytest.mark.parametrize(
    ("worker_id", "version", "lease", "poll", "retry_base", "retry_max", "message"),
    [
        ("", "1.0.0", 30, 1.0, 1, 60, "worker_id"),
        ("worker-a", " ", 30, 1.0, 1, 60, "version"),
        ("worker-a", "1.0.0", 0, 1.0, 1, 60, "command_lease_seconds"),
        ("worker-a", "1.0.0", 3601, 1.0, 1, 60, "command_lease_seconds"),
        ("worker-a", "1.0.0", 30, 0.0, 1, 60, "poll_seconds"),
        ("worker-a", "1.0.0", 30, 301.0, 1, 60, "poll_seconds"),
        ("worker-a", "1.0.0", 30, float("nan"), 1, 60, "poll_seconds"),
        ("worker-a", "1.0.0", 30, float("inf"), 1, 60, "poll_seconds"),
        ("worker-a", "1.0.0", 30, float("-inf"), 1, 60, "poll_seconds"),
        ("worker-a", "1.0.0", 30, 1.0, 0, 60, "retry_base_seconds"),
        ("worker-a", "1.0.0", 30, 1.0, 10, 5, "retry_max_seconds"),
    ],
)
def test_worker_rejects_invalid_constructor_bounds(
    worker_id: str,
    version: str,
    lease: int,
    poll: float,
    retry_base: int,
    retry_max: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        TradingWorker(
            ExplodingCommands(),
            RecordingOperations(),
            worker_id=worker_id,
            version=version,
            command_lease_seconds=lease,
            poll_seconds=poll,
            retry_base_seconds=retry_base,
            retry_max_seconds=retry_max,
        )


def test_worker_entrypoint_parses_typed_arguments() -> None:
    module = _load_worker_script()
    parse_args = cast(Callable[[Sequence[str] | None], object], module.parse_args)

    parsed = parse_args(("--worker-id", "worker-a", "--poll-seconds", "2.5", "--once"))

    assert vars(parsed) == {"worker_id": "worker-a", "poll_seconds": 2.5, "once": True}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ((), "--worker-id"),
        (("--worker-id", " "), "worker id must not be blank"),
        (("--worker-id", "worker/a"), "letters, numbers"),
        (("--worker-id", "worker-a", "--poll-seconds", "0"), "greater than 0"),
        (("--worker-id", "worker-a", "--poll-seconds", "301"), "at most 300"),
    ],
)
def test_worker_entrypoint_rejects_invalid_arguments(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    script_path = Path(__file__).parents[2] / "scripts" / "run_trading_worker.py"

    result = subprocess.run(
        [sys.executable, str(script_path), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert message in result.stderr


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_worker_entrypoint_rejects_non_finite_poll_before_runtime_construction(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    module = _load_worker_script()
    settings_calls = 0
    engine_calls = 0

    def fail_settings() -> object:
        nonlocal settings_calls
        settings_calls += 1
        raise AssertionError("settings must not be constructed")

    def fail_engine(_settings: object) -> object:
        nonlocal engine_calls
        engine_calls += 1
        raise AssertionError("engine must not be constructed")

    monkeypatch.setattr(module, "PlatformSettings", fail_settings)
    monkeypatch.setattr(module, "create_platform_engine", fail_engine)
    main = cast(Callable[[Sequence[str] | None], None], module.main)

    with pytest.raises(SystemExit) as raised:
        main(("--worker-id", "worker-a", "--poll-seconds", value))

    assert raised.value.code == 2
    assert settings_calls == 0
    assert engine_calls == 0


def test_worker_entrypoint_wires_once_and_disposes_owned_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_worker_script()
    captured, runtime, engine = _install_cli_fakes(module, monkeypatch)
    main = cast(Callable[[Sequence[str] | None], None], module.main)

    main(("--worker-id", "worker-a", "--poll-seconds", "2.5", "--once"))

    assert runtime.run_once_calls == 1
    assert runtime.run_forever_calls == 0
    assert captured["worker_kwargs"] == {
        "worker_id": "worker-a",
        "version": "0.test",
        "command_lease_seconds": 45,
        "poll_seconds": 2.5,
    }
    assert engine.dispose_calls == 1


def test_worker_entrypoint_signals_stop_forever_worker_and_restores_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_worker_script()
    _captured, runtime, engine = _install_cli_fakes(module, monkeypatch)
    current_handlers: dict[signal.Signals, object] = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    initial_handlers = dict(current_handlers)

    def fake_signal(signum: signal.Signals, handler: object) -> object:
        previous = current_handlers[signum]
        current_handlers[signum] = handler
        return previous

    def invoke_forever() -> None:
        runtime.run_forever_calls += 1
        handler = cast(Callable[[int, FrameType | None], None], current_handlers[signal.SIGTERM])
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(runtime, "run_forever", invoke_forever)
    monkeypatch.setattr(module.signal, "signal", fake_signal)
    main = cast(Callable[[Sequence[str] | None], None], module.main)

    main(("--worker-id", "worker-a"))

    assert runtime.run_forever_calls == 1
    assert runtime.stop_calls == 1
    assert current_handlers == initial_handlers
    assert engine.dispose_calls == 1


def test_worker_entrypoint_disposes_engine_when_worker_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_worker_script()
    _captured, runtime, engine = _install_cli_fakes(module, monkeypatch)

    def fail_once() -> bool:
        runtime.run_once_calls += 1
        raise RuntimeError("worker failed")

    monkeypatch.setattr(runtime, "run_once", fail_once)
    main = cast(Callable[[Sequence[str] | None], None], module.main)

    with pytest.raises(RuntimeError, match="worker failed"):
        main(("--worker-id", "worker-a", "--once"))

    assert engine.dispose_calls == 1


def test_worker_modules_are_strategy_and_broker_free() -> None:
    root = Path(__file__).parents[2]
    paths = (root / "src/qt/platform/worker.py", root / "scripts/run_trading_worker.py")

    for path in paths:
        source = path.read_text(encoding="utf-8")
        imports = _imports(source)
        assert "strategy" not in source.lower()
        assert "broker" not in source.lower()
        assert not any(module.startswith("qt.strateg") for module in imports)
        assert not any(module.startswith("qt.execution") for module in imports)


class FakeEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


class FakeRuntimeWorker:
    def __init__(self) -> None:
        self.run_once_calls = 0
        self.run_forever_calls = 0
        self.stop_calls = 0

    def run_once(self) -> bool:
        self.run_once_calls += 1
        return False

    def run_forever(self) -> None:
        self.run_forever_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


def _install_cli_fakes(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, object], FakeRuntimeWorker, FakeEngine]:
    captured: dict[str, object] = {}
    runtime = FakeRuntimeWorker()
    engine = FakeEngine()
    settings = PlatformSettings(
        platform_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        command_lease_seconds=45,
        worker_stale_seconds=60,
        _env_file=None,  # type: ignore[call-arg]
    )

    def fake_session_factory(_engine: object) -> object:
        return "sessions"

    def fake_commands(session_factory: object) -> object:
        captured["command_sessions"] = session_factory
        return "commands"

    def fake_operations(session_factory: object) -> object:
        captured["operation_sessions"] = session_factory
        return "operations"

    def fake_worker(
        commands: object,
        operations: object,
        **kwargs: object,
    ) -> FakeRuntimeWorker:
        captured["commands"] = commands
        captured["operations"] = operations
        captured["worker_kwargs"] = kwargs
        return runtime

    monkeypatch.setattr(module, "PlatformSettings", lambda: settings)
    monkeypatch.setattr(module, "create_platform_engine", lambda _settings: engine)
    monkeypatch.setattr(module, "create_session_factory", fake_session_factory)
    monkeypatch.setattr(module, "CommandRepository", fake_commands)
    monkeypatch.setattr(module, "OperationsRepository", fake_operations)
    monkeypatch.setattr(module, "TradingWorker", fake_worker)
    monkeypatch.setattr(module, "__version__", "0.test")
    return captured, runtime, engine


def _load_worker_script() -> ModuleType:
    script_path = Path(__file__).parents[2] / "scripts" / "run_trading_worker.py"
    spec = importlib.util.spec_from_file_location("qt_task6_run_trading_worker", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load trading worker entrypoint")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _imports(source: str) -> set[str]:
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
    return imports
