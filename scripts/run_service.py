"""Run QT with a parent watchdog that keeps child processes alive."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

from qt.monitoring.health import evaluate_monitor_health

console = Console()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/default.yaml")
    p.add_argument("--interval", type=int, default=3600, help="paper-loop seconds between cycles")
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--state-path", default="data/runtime/monitor_state.json")
    p.add_argument("--stale-after-seconds", type=int, default=7200)
    p.add_argument("--startup-grace-seconds", type=int, default=300)
    p.add_argument("--check-interval", type=int, default=60)
    p.add_argument("--dashboard-host", default="127.0.0.1")
    p.add_argument("--dashboard-port", type=int, default=8765)
    p.add_argument("--backtests-dir", default="data/backtests")
    p.add_argument("--no-dashboard", action="store_true")
    args = p.parse_args()

    state_path = Path(args.state_path)
    paper = _start_paper(args)
    paper_started_at = time.monotonic()
    dashboard = None if args.no_dashboard else _start_dashboard(args)

    try:
        while True:
            if paper.poll() is not None:
                console.print(f"[yellow]paper exited with code {paper.returncode}; restarting[/]")
                paper = _start_paper(args)
                paper_started_at = time.monotonic()

            if dashboard is not None and dashboard.poll() is not None:
                console.print(
                    f"[yellow]dashboard exited with code {dashboard.returncode}; restarting[/]"
                )
                dashboard = _start_dashboard(args)

            health = evaluate_monitor_health(
                state_path,
                stale_after_seconds=args.stale_after_seconds,
            )
            missing_after_grace = (
                health.status == "missing"
                and time.monotonic() - paper_started_at > args.startup_grace_seconds
            )
            if health.status in {"stale", "failed", "invalid", "stopped"} or missing_after_grace:
                console.print(f"[red]{health.message}; restarting paper loop[/]")
                _stop_process(paper)
                paper = _start_paper(args)
                paper_started_at = time.monotonic()

            time.sleep(args.check_interval)
    except KeyboardInterrupt:
        console.print("[yellow]stopping QT service[/]")
    finally:
        _stop_process(paper)
        if dashboard is not None:
            _stop_process(dashboard)


def _start_paper(args: argparse.Namespace) -> subprocess.Popen[bytes]:
    cmd = [
        sys.executable,
        "scripts/run_paper.py",
        "--config",
        str(args.config),
        "--interval",
        str(args.interval),
        "--cash",
        str(args.cash),
        "--state-path",
        str(args.state_path),
    ]
    console.print(f"[green]starting paper[/] {' '.join(cmd)}")
    return subprocess.Popen(cmd)


def _start_dashboard(args: argparse.Namespace) -> subprocess.Popen[bytes]:
    cmd = [
        sys.executable,
        "scripts/run_dashboard.py",
        "--config",
        str(args.config),
        "--host",
        str(args.dashboard_host),
        "--port",
        str(args.dashboard_port),
        "--backtests-dir",
        str(args.backtests_dir),
        "--monitor-state",
        str(args.state_path),
    ]
    console.print(f"[green]starting dashboard[/] http://{args.dashboard_host}:{args.dashboard_port}")
    return subprocess.Popen(cmd)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


if __name__ == "__main__":
    main()
