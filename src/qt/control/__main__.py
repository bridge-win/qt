"""Local process entry points. Configuration is explicit; no implicit migrations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import threading
from contextlib import suppress
from pathlib import Path

from qt.control.service import ControlService
from qt.persistence import Repository, create_engine, migrate


def configured_service() -> ControlService:
    url = os.environ.get("QT_V4_DATABASE_URL", "")
    return ControlService(Repository(create_engine(url)),
                           Path(os.environ.get("QT_V4_DATA_ROOT", "data/runtime/production-v4/datasets")),
                           tenant=os.environ.get("QT_V4_TENANT", "personal"),
                           signing_key=os.environ.get("QT_V4_SIGNING_KEY", "").encode())


async def collect(service: ControlService, *, trading: bool = False, once: bool = False) -> None:
    from qt.contracts import ExecutableMarketSnapshot
    from qt.marketdata.collector import BinanceCollector

    repo, tenant = service.repository, service.tenant
    runtime, instrument, fees = None, None, None
    if trading:
        identity = os.environ.get("QT_V4_DEPLOYMENT_ID", "")
        if not identity:
            repo.health(tenant, "trading-runtime", {"status": "blocked", "reason": "deployment_id_required"})
            if not once:
                await asyncio.Event().wait()
            return
        from qt.execution.runtime import runtime_for_deployment

        runtime, instrument, fees = runtime_for_deployment(service, identity)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)

    async def event_callback(event, book):
        if runtime is None or book is None or not book.bids or not book.asks:
            return
        market = ExecutableMarketSnapshot(
            venue=event.venue, instrument=event.instrument,
            bid=book.bids[0].price, ask=book.asks[0].price,
            asof_ns=book.received_ts_ns, book_valid=True,
            instrument_spec_hash=instrument.content_hash(), fee_schedule_hash=fees.content_hash(),
        )
        # A durable current mark is shared with the independent watchdog, not
        # with any model. It is invalidated immediately on feed degradation.
        repo.health(tenant, f"market:{event.venue}:{event.instrument}", market.model_dump(mode="json"))
        outcome = await runtime.process(event, market, book=book)
        repo.health(tenant, "trading-runtime", outcome)
        if once:
            stop.set()

    def health_callback(health):
        repo.health(tenant, f"feed:{health['venue']}:{health['instrument']}", health)
        if health.get("status") != "ready" or not health.get("book_valid"):
            repo.health(tenant, f"market:{health['venue']}:{health['instrument']}",
                        {"book_valid": False, "reason": health.get("reason"), "status": health["status"]})

    collector = BinanceCollector(
        service.datasets, symbol=os.environ.get("QT_V4_SYMBOL", "BTCUSDT"),
        bar_interval=os.environ.get("QT_V4_BAR_INTERVAL", "1m"),
        on_event=event_callback, on_health=health_callback,
        on_manifest=lambda manifest: service.register("dataset", manifest),
    )

    async def reconciliation_loop():
        from qt.control.worker import reconcile_paper_once

        while not stop.is_set():
            try:
                # Reconciliation owns no send authority. Run its blocking DB
                # calls off the market event loop, within this OMS process.
                await asyncio.to_thread(lambda: asyncio.run(reconcile_paper_once(service)))
            except Exception as error:
                repo.health(tenant, "reconciliation", {"status": "blocked", "error_type": type(error).__name__})
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1)

    reconciliation = asyncio.create_task(reconciliation_loop()) if runtime is not None else None
    try:
        await collector.run(stop=stop, maximum_sessions=1 if once else None)
    finally:
        stop.set()
        if reconciliation is not None:
            await reconciliation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "api", "check", "research-worker",
                                          "experiment-worker", "data-worker", "trading-runtime",
                                          "watchdog", "reconciliation-worker", "import-legacy", "ingest-evidence"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    service = configured_service()
    repo, tenant = service.repository, service.tenant
    if args.command == "migrate":
        from qt.persistence.database import migrate_checkpoints

        migrate(repo.engine)
        migrate_checkpoints(repo.engine)
        print(json.dumps({"status": "migrated", "schema": "qt_v4", "checkpoints": "installed"}))
        return 0
    if args.command == "api":
        import uvicorn

        from qt.control.api import create_app

        uvicorn.run(create_app(service), host="0.0.0.0", port=8890, access_log=False)
        return 0
    if args.command == "check":
        print(json.dumps(service.readiness(), allow_nan=False))
        return 0
    if args.command == "import-legacy":
        from qt.persistence.legacy import import_sqlite_archive

        if args.path is None:
            parser.error("--path is required")
        print(json.dumps(import_sqlite_archive(repo, tenant, args.path), allow_nan=False))
        return 0
    if args.command == "ingest-evidence":
        from qt.research.evidence import EvidenceDocument, EvidenceRepository

        if args.path is None:
            parser.error("--path is required")
        document = EvidenceDocument.model_validate_json(args.path.read_text())
        row = EvidenceRepository(repo).ingest(tenant, document)
        print(json.dumps({"evidence_id": row["entity_id"], "sha256": row["sha256"]}))
        return 0
    if args.command in {"data-worker", "trading-runtime"}:
        asyncio.run(collect(service, trading=args.command == "trading-runtime", once=args.once))
        return 0
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    if args.command == "watchdog":
        from qt.operations.watchdog import Watchdog

        worker = Watchdog(service)
        while not stop.is_set():
            try:
                result = worker.check()
                repo.health(tenant, "watchdog", result)
            except Exception as error:
                result = {"status": "blocked", "error_type": type(error).__name__}
            if args.once:
                print(json.dumps(result, default=str))
                return 0
            stop.wait(1)
        return 0
    from qt.control.worker import provider_from_environment, run_once

    kind = args.command.removesuffix("-worker")
    while not stop.is_set():
        try:
            if kind == "reconciliation":
                from qt.control.worker import reconcile_paper_once

                result = asyncio.run(reconcile_paper_once(service))
            else:
                provider = provider_from_environment() if kind == "research" else None
                result = run_once(service, kind, provider=provider)
            repo.health(tenant, args.command, {"status": "ready", "processed_job": bool(result)})
        except (KeyError, ValueError) as error:
            result = {"status": "blocked", "error_type": type(error).__name__,
                      "reason": "explicit_runtime_configuration_required"}
            repo.health(tenant, args.command, result)
        if args.once:
            print(json.dumps(result, default=str))
            return 0
        stop.wait(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
