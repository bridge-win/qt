from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine

from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, create_session_factory
from qt.platform.models import Base
from qt.platform.operations import OperationsRepository
from qt.platform.schemas import WorkerStatus


@pytest.fixture
def settings(tmp_path: Path) -> PlatformSettings:
    return PlatformSettings(
        platform_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'probe.db'}",
        worker_stale_seconds=60,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def engine(settings: PlatformSettings) -> Iterator[Engine]:
    database_engine = create_platform_engine(settings)
    Base.metadata.create_all(database_engine)
    yield database_engine
    database_engine.dispose()


def test_api_probe_requires_live_and_ready_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    from qt.platform import probe

    responses = {
        "http://api:8876/api/health/live": {"status": "alive"},
        "http://api:8876/api/health/ready": {"status": "ready"},
    }
    monkeypatch.setattr(probe, "_read_json", responses.__getitem__)

    assert probe.api_probe("http://api:8876") is True

    responses["http://api:8876/api/health/ready"] = {"status": "not_ready"}
    assert probe.api_probe("http://api:8876") is False


def test_api_probe_redacts_transport_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from qt.platform import probe

    def unavailable(_url: str) -> dict[str, object]:
        raise OSError("sensitive transport detail")

    monkeypatch.setattr(probe, "_read_json", unavailable)

    assert probe.api_probe("http://api:8876") is False


def test_worker_probe_requires_fresh_healthy_durable_heartbeat(
    settings: PlatformSettings,
    engine: Engine,
) -> None:
    from qt.platform.probe import worker_probe

    session_factory = create_session_factory(engine)
    OperationsRepository(session_factory).record_heartbeat(
        role="trading",
        instance_id="trading-1",
        status=WorkerStatus.HEALTHY,
        version="1.0.0",
        details={"state": "idle"},
    )

    assert worker_probe(settings, role="trading", instance_id="trading-1") is True
    assert worker_probe(settings, role="trading", instance_id="missing") is False


@pytest.mark.parametrize("status", [WorkerStatus.DEGRADED, WorkerStatus.FAILED])
def test_worker_probe_rejects_fresh_non_healthy_status(
    settings: PlatformSettings,
    engine: Engine,
    status: WorkerStatus,
) -> None:
    from qt.platform.probe import worker_probe

    session_factory = create_session_factory(engine)
    OperationsRepository(session_factory).record_heartbeat(
        role="trading",
        instance_id="trading-1",
        status=status,
        version="1.0.0",
        details={},
    )

    assert worker_probe(settings, role="trading", instance_id="trading-1") is False


def test_worker_probe_rejects_stale_heartbeat(
    settings: PlatformSettings,
    engine: Engine,
) -> None:
    from qt.platform.probe import worker_probe

    def old_clock() -> datetime:
        return datetime.now(timezone.utc) - timedelta(seconds=61)

    OperationsRepository(create_session_factory(engine), clock=old_clock).record_heartbeat(
        role="trading",
        instance_id="trading-1",
        status=WorkerStatus.HEALTHY,
        version="1.0.0",
        details={},
    )

    assert worker_probe(settings, role="trading", instance_id="trading-1") is False


def test_worker_probe_redacts_engine_construction_failure(
    settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qt.platform import probe

    def reject_settings(_settings: PlatformSettings) -> Engine:
        raise RuntimeError("sensitive database URL")

    monkeypatch.setattr(probe, "create_platform_engine", reject_settings)

    assert probe.worker_probe(settings, role="trading", instance_id="trading-1") is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_probe_cli_rejects_unknown_or_incomplete_roles() -> None:
    from qt.platform.probe import main

    with pytest.raises(SystemExit):
        main(["unknown"])
    with pytest.raises(SystemExit):
        main(["worker", "--role", "trading"])
