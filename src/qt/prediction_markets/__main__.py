"""One-shot public snapshot. An exchange testnet or trading mode is not supported."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .polymarket import PublicDataError, fetch_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Polymarket outcome prices (not BTC spot)")
    parser.add_argument("--slug", required=True)
    args = parser.parse_args()
    try:
        print(asyncio.run(fetch_snapshot(args.slug)).model_dump_json())
        return 0
    except PublicDataError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
