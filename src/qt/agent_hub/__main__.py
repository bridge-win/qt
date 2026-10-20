"""Container entrypoints for API, scheduler, workers, Codex and offline sandbox."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import threading
import time
from pathlib import Path

from qt.agent_hub.config import files_root, load_config, state_root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["api", "worker", "scheduler", "codex", "sandbox", "health", "once"]
    )
    parser.add_argument("--worker-id", default="worker-1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--kinds", default="")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # HTTP client logs include upstream URLs; business events carry bounded identifiers instead.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.worker_id):
        parser.error("invalid worker id")
    if args.command in {"api", "codex", "sandbox"}:
        import uvicorn

        if args.command == "api":
            from qt.agent_hub.api import create_app

            app = create_app(load_config(), state_root(), files_root())
            port = args.port or 8878
        elif args.command == "codex":
            from qt.agent_hub.codex import seed_oauth
            from qt.agent_hub.codex_service import create_codex_app

            home = Path(os.environ.get("QT_AGENT_HUB_CODEX_HOME", "/codex-home"))
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = Path("/run/secrets/codex_auth")
            if source.is_file():
                seed_oauth(home, source)
            app, port = create_codex_app(), args.port or 8091
        else:
            from qt.agent_hub.sandbox_service import create_sandbox_app

            app, port = create_sandbox_app(), args.port or 8092
        uvicorn.run(app, host="0.0.0.0", port=port, access_log=False)
        return
    if args.command == "health":
        path = state_root() / "heartbeats" / f"{args.worker_id}.json"
        healthy = path.is_file() and time.time() - json.loads(path.read_text())["timestamp"] < 60
        raise SystemExit(0 if healthy else 1)
    from qt.agent_hub.runtime import HubRuntime

    runtime = HubRuntime(load_config(), state_root(), files_root())
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if args.command == "scheduler":
        runtime.install_schedules()
    if args.command == "once":
        print(
            json.dumps(
                runtime.run_once(args.worker_id, args.kinds.split(",") if args.kinds else None)
            )
        )
        return

    def beats() -> None:
        while not stop.is_set():
            runtime.heartbeat_file(args.worker_id)
            stop.wait(10)

    heartbeat = threading.Thread(target=beats, daemon=True)
    heartbeat.start()
    while not stop.is_set():
        try:
            if args.command == "scheduler":
                runtime.store.tick_schedules(runtime.config.tenant_id)
                stop.wait(2)
            else:
                job = runtime.run_once(
                    args.worker_id, args.kinds.split(",") if args.kinds else None
                )
                if not job:
                    stop.wait(1)
        except Exception as error:
            logging.getLogger(__name__).warning("loop_failure error_type=%s", type(error).__name__)
            stop.wait(5)
    heartbeat.join(timeout=2)


if __name__ == "__main__":
    main()
