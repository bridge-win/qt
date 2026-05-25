"""Run every enabled strategy + the dashboard in a single process.

One-line entry point for the full QT solution gallery:

    python scripts/run_all.py

Each strategy YAML in ``config/strategies/`` is loaded; one daemon
thread per enabled strategy is started; the dashboard HTTP server runs
in the main thread (so Ctrl-C cleanly stops everything).

Opportunities trigger ``qt.monitoring.alerts.alert(...)`` which writes
to stderr and, when ``QT_SMTP_*`` / ``QT_TELEGRAM_*`` are configured,
also sends email + Telegram. Each strategy persists a heartbeat to
``data/runtime/strategies/<name>.json`` that the dashboard reads.
"""

from __future__ import annotations

import argparse
import threading
from pathlib import Path

from rich.console import Console

from qt.core.config import load_settings
from qt.core.logging import configure_logging
from qt.dashboard import serve_dashboard
from qt.strategies import (
    build_strategies,
    load_strategy_configs,
    start_all_strategies,
)

console = Console()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config/default.yaml")
    p.add_argument("--strategies-dir", default="config/strategies")
    p.add_argument("--runtime-dir", default="data/runtime")
    p.add_argument("--dashboard-host", default="127.0.0.1")
    p.add_argument("--dashboard-port", type=int, default=8765)
    p.add_argument("--backtests-dir", default="data/backtests")
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    configure_logging(args.log_level)
    settings = load_settings(args.config)
    configs = load_strategy_configs(args.strategies_dir)
    strategies = build_strategies(configs)
    if not strategies:
        console.print(
            f"[yellow]no enabled strategies found in {args.strategies_dir}[/]"
        )
        return

    runtime_dir = Path(args.runtime_dir)
    (runtime_dir / "strategies").mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    threads = start_all_strategies(
        strategies, settings,
        runtime_dir=runtime_dir, stop_event=stop,
    )
    for s in strategies:
        console.print(
            f"[green]started[/] {s.name} "
            f"interval={s.config.interval_seconds}s "
            f"alert={s.config.min_alert_severity}"
        )

    if args.no_dashboard:
        import contextlib

        console.print("[yellow]dashboard disabled; ctrl-c to stop[/]")
        with contextlib.suppress(KeyboardInterrupt):
            stop.wait()
        stop.set()
        for t in threads:
            t.join(timeout=10)
        return

    monitor_state = runtime_dir / "monitor_state.json"
    console.print(
        f"[green]dashboard[/] http://{args.dashboard_host}:{args.dashboard_port}"
    )
    try:
        serve_dashboard(
            host=args.dashboard_host,
            port=args.dashboard_port,
            parquet_dir=settings.data.parquet_dir,
            backtests_dir=args.backtests_dir,
            monitor_state_path=monitor_state,
            strategies_state_dir=runtime_dir / "strategies",
        )
    except KeyboardInterrupt:
        console.print("[yellow]shutting down[/]")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)


if __name__ == "__main__":
    main()
