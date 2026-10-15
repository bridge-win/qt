"""Standalone CLI. Never imports workbench, brokers, or authenticated trading code."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .config import MonitorConfig
from .service import health_status, read_status, run_monitor, self_test


def main() -> int:
    parser = argparse.ArgumentParser(description="Public spot BTC monitoring and grid research")
    parser.add_argument("command", choices=("run", "status", "health", "self-test"))
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "self-test":
            print(json.dumps(asyncio.run(self_test())))
            return 0
        config = MonitorConfig.from_env(args.state_dir)
        if args.command == "run":
            asyncio.run(run_monitor(config))
        else:
            snapshot = read_status(config.state_dir)
            if args.command == "status":
                print(snapshot.model_dump_json())
            else:
                ok = health_status(snapshot, time.time())
                print(
                    json.dumps(
                        {
                            "alive": ok,
                            "data_ready": snapshot.health.data_ready,
                            "assessment_ready": snapshot.health.assessment_ready,
                        }
                    )
                )
                return 0 if ok else 1
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Never interpolate exception text: URLs/secrets can be embedded upstream.
        print("btc_monitor_operation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
