"""Dependency-light container probes for platform API and worker health."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import cast
from urllib.request import urlopen

from sqlalchemy import Engine

from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, create_session_factory
from qt.platform.health import ExpectedWorker, HealthService, WorkerHealthStatus
from qt.platform.operations import OperationsRepository

_HTTP_TIMEOUT_SECONDS = 5.0


def api_probe(base_url: str) -> bool:
    """Require both process liveness and database-backed API readiness."""

    normalized = base_url.rstrip("/")
    try:
        live = _read_json(f"{normalized}/api/health/live")
        ready = _read_json(f"{normalized}/api/health/ready")
    except Exception:
        return False
    return live.get("status") == "alive" and ready.get("status") == "ready"


def worker_probe(settings: PlatformSettings, *, role: str, instance_id: str) -> bool:
    """Require a reachable database and one fresh healthy durable heartbeat."""

    engine: Engine | None = None
    try:
        engine = create_platform_engine(settings)
        session_factory = create_session_factory(engine)
        health = HealthService(
            session_factory,
            OperationsRepository(session_factory),
            expected_workers=(ExpectedWorker(role=role, instance_id=instance_id),),
            stale_after_seconds=settings.worker_stale_seconds,
        )
        if health.readiness().status != "ready":
            return False
        report = health.worker_health()
        return (
            report.status == "healthy"
            and len(report.workers) == 1
            and report.workers[0].status is WorkerHealthStatus.HEALTHY
        )
    except Exception:
        return False
    finally:
        if engine is not None:
            engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe QT platform container health")
    subparsers = parser.add_subparsers(dest="probe", required=True)

    api_parser = subparsers.add_parser("api", help="probe API liveness and readiness")
    api_parser.add_argument("--base-url", required=True)

    worker_parser = subparsers.add_parser("worker", help="probe one durable worker heartbeat")
    worker_parser.add_argument("--role", required=True)
    worker_parser.add_argument("--instance-id", required=True)

    namespace = parser.parse_args(argv)
    if namespace.probe == "api":
        healthy = api_probe(str(namespace.base_url))
    else:
        try:
            settings = PlatformSettings()
            healthy = worker_probe(
                settings,
                role=str(namespace.role),
                instance_id=str(namespace.instance_id),
            )
        except Exception:
            healthy = False
    if not healthy:
        print(f"{namespace.probe} health probe failed", file=sys.stderr)
        return 1
    return 0


def _read_json(url: str) -> dict[str, object]:
    with urlopen(url, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("health endpoint must return a JSON object")
    return cast(dict[str, object], payload)


if __name__ == "__main__":
    raise SystemExit(main())
