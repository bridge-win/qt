"""Run the research-only v3 workbench API on a Linux compute node."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from qt.workbench.api import WorkbenchSettings, create_workbench_app


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the QT v3 research workbench API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--parquet-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument(
        "--origin-client-id",
        default=os.environ.get("QT_WORKBENCH_ORIGIN_CLIENT_ID"),
        help="Worker-to-origin client ID; normally supplied by a protected EnvironmentFile",
    )
    parser.add_argument(
        "--origin-client-secret",
        default=os.environ.get("QT_WORKBENCH_ORIGIN_CLIENT_SECRET"),
        help="Worker-to-origin client secret; normally supplied by a protected EnvironmentFile",
    )
    args = parser.parse_args(argv)
    if bool(args.origin_client_id) != bool(args.origin_client_secret):
        parser.error("origin client ID and secret must be supplied together")
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.origin_client_id:
        parser.error("a non-loopback API binding requires Worker-to-origin credentials")
    uvicorn.run(
        create_workbench_app(
            WorkbenchSettings(
                parquet_root=args.parquet_root,
                state_root=args.state_root,
                origin_client_id=args.origin_client_id,
                origin_client_secret=args.origin_client_secret,
            )
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
