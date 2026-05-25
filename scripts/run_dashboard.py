"""Serve the local QT dashboard."""

from __future__ import annotations

import argparse

from rich.console import Console

from qt.core.config import load_settings
from qt.dashboard import serve_dashboard


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/default.yaml")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--backtests-dir", default="data/backtests")
    p.add_argument("--monitor-state", default="data/runtime/monitor_state.json")
    p.add_argument("--strategies-state-dir", default="data/runtime/strategies")
    args = p.parse_args()

    settings = load_settings(args.config)
    Console().print(f"[green]dashboard[/] http://{args.host}:{args.port}")
    serve_dashboard(
        host=args.host,
        port=args.port,
        parquet_dir=settings.data.parquet_dir,
        backtests_dir=args.backtests_dir,
        monitor_state_path=args.monitor_state,
        strategies_state_dir=args.strategies_state_dir,
    )


if __name__ == "__main__":
    main()
