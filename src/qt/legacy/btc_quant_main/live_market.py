# Migrated with Python 3.10 UTC compatibility from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
import math
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CCXT_SNAPSHOT_SCRIPT = """
import json
import sys

import ccxt

exchange_id, symbol, limit_text = sys.argv[1:4]
exchange_class = getattr(ccxt, exchange_id)
exchange = exchange_class({"enableRateLimit": True})
limit = int(limit_text)
ticker = exchange.fetch_ticker(symbol)
orderbook = exchange.fetch_order_book(symbol, limit=limit)

payload = {
    "orderbook": {
        "asks": orderbook.get("asks", [])[:limit],
        "bids": orderbook.get("bids", [])[:limit],
        "datetime": orderbook.get("datetime"),
        "nonce": orderbook.get("nonce"),
        "timestamp": orderbook.get("timestamp"),
    },
    "ticker": {
        "ask": ticker.get("ask"),
        "bid": ticker.get("bid"),
        "datetime": ticker.get("datetime"),
        "last": ticker.get("last"),
        "timestamp": ticker.get("timestamp"),
    },
}
print(json.dumps(payload, sort_keys=True))
""".strip()


@dataclass(frozen=True)
class LiveMarketPolicy:
    max_ticker_staleness_seconds: float
    max_orderbook_staleness_seconds: float
    max_spread_bps: float
    min_orderbook_levels: int
    max_clock_skew_seconds: float = 5

    def __post_init__(self) -> None:
        if self.max_ticker_staleness_seconds < 0:
            raise ValueError("max_ticker_staleness_seconds cannot be negative")
        if self.max_orderbook_staleness_seconds < 0:
            raise ValueError("max_orderbook_staleness_seconds cannot be negative")
        if self.max_spread_bps < 0:
            raise ValueError("max_spread_bps cannot be negative")
        if self.min_orderbook_levels <= 0:
            raise ValueError("min_orderbook_levels must be positive")
        if self.max_clock_skew_seconds < 0:
            raise ValueError("max_clock_skew_seconds cannot be negative")


def build_live_market_command(*, exchange: str, pair: str, orderbook_limit: int) -> list[str]:
    return [
        "docker",
        "compose",
        "run",
        "--rm",
        "--entrypoint",
        "python",
        "freqtrade",
        "-c",
        CCXT_SNAPSHOT_SCRIPT,
        exchange,
        pair,
        str(orderbook_limit),
    ]


def fetch_live_market_snapshot(
    *,
    exchange: str,
    pair: str,
    orderbook_limit: int,
    root_dir: Path,
) -> dict[str, Any]:
    completed = subprocess.run(
        build_live_market_command(exchange=exchange, pair=pair, orderbook_limit=orderbook_limit),
        cwd=root_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
        text=True,
    )
    return parse_live_market_stdout(completed.stdout)


def parse_live_market_stdout(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("live market snapshot command did not return a JSON object")


def evaluate_live_market_snapshot(
    *,
    snapshot: dict[str, Any],
    captured_at: datetime,
    policy: LiveMarketPolicy,
) -> dict[str, Any]:
    ticker = _object(snapshot.get("ticker"))
    orderbook = _object(snapshot.get("orderbook"))
    bids = _levels(orderbook.get("bids"))
    asks = _levels(orderbook.get("asks"))
    ticker_bid = _finite_float(ticker.get("bid"))
    ticker_ask = _finite_float(ticker.get("ask"))
    last = _finite_float(ticker.get("last"))
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    ticker_staleness_seconds = _staleness_seconds(ticker.get("timestamp"), captured_at=captured_at)
    orderbook_staleness_seconds = _staleness_seconds(orderbook.get("timestamp"), captured_at=captured_at)
    spread_bps = _spread_bps(ticker_bid, ticker_ask)
    failed_reasons: list[str] = []

    if ticker_staleness_seconds is not None and ticker_staleness_seconds < -policy.max_clock_skew_seconds:
        failed_reasons.append(f"ticker timestamp is {-ticker_staleness_seconds:.2f} seconds ahead of local clock")
    if (
        orderbook_staleness_seconds is not None
        and orderbook_staleness_seconds < -policy.max_clock_skew_seconds
    ):
        failed_reasons.append(
            f"orderbook timestamp is {-orderbook_staleness_seconds:.2f} seconds ahead of local clock"
        )
    if ticker_staleness_seconds is not None and ticker_staleness_seconds > policy.max_ticker_staleness_seconds:
        failed_reasons.append(f"ticker stale by {ticker_staleness_seconds:.2f} seconds")
    if (
        orderbook_staleness_seconds is not None
        and orderbook_staleness_seconds > policy.max_orderbook_staleness_seconds
    ):
        failed_reasons.append(f"orderbook stale by {orderbook_staleness_seconds:.2f} seconds")
    if len(bids) < policy.min_orderbook_levels:
        failed_reasons.append(f"orderbook has fewer than {policy.min_orderbook_levels} bid levels")
    if len(asks) < policy.min_orderbook_levels:
        failed_reasons.append(f"orderbook has fewer than {policy.min_orderbook_levels} ask levels")
    if ticker_bid is None:
        failed_reasons.append("ticker bid is missing or invalid")
    if ticker_ask is None:
        failed_reasons.append("ticker ask is missing or invalid")
    if best_bid is None:
        failed_reasons.append("orderbook best bid is missing or invalid")
    if best_ask is None:
        failed_reasons.append("orderbook best ask is missing or invalid")
    if best_bid is not None and best_ask is not None and best_bid > best_ask:
        failed_reasons.append("best bid is above best ask")
    if spread_bps is not None and spread_bps > policy.max_spread_bps:
        failed_reasons.append(f"ticker spread above {policy.max_spread_bps:g} bps")
    if bids and bids[0][1] <= 0:
        failed_reasons.append("top bid amount is not positive")
    if asks and asks[0][1] <= 0:
        failed_reasons.append("top ask amount is not positive")

    return {
        "best_ask": best_ask,
        "best_bid": best_bid,
        "failed_reasons": failed_reasons,
        "last": last,
        "orderbook_staleness_seconds": orderbook_staleness_seconds,
        "passed": not failed_reasons,
        "spread_bps": spread_bps,
        "ticker_staleness_seconds": ticker_staleness_seconds,
    }


def write_live_market_report(
    *,
    output_dir: Path,
    exchange: str,
    pair: str,
    captured_at: datetime,
    policy: LiveMarketPolicy,
    snapshot: dict[str, Any],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_captured_at = _normalize_datetime(captured_at)
    report = {
        "captured_at": _format_timestamp(normalized_captured_at),
        "exchange": exchange,
        "pair": pair,
        "policy": asdict(policy),
        "result": evaluate_live_market_snapshot(
            snapshot=snapshot,
            captured_at=normalized_captured_at,
            policy=policy,
        ),
        "snapshot": snapshot,
    }
    report_path = output_dir / "live_market.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report_path


def run_live_market_check(
    *,
    output_dir: Path,
    exchange: str,
    pair: str,
    policy: LiveMarketPolicy,
    root_dir: Path,
) -> Path:
    snapshot = fetch_live_market_snapshot(
        exchange=exchange,
        pair=pair,
        orderbook_limit=policy.min_orderbook_levels,
        root_dir=root_dir,
    )
    return write_live_market_report(
        output_dir=output_dir,
        exchange=exchange,
        pair=pair,
        captured_at=datetime.now(timezone.utc),
        policy=policy,
        snapshot=snapshot,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture and validate a live public market snapshot.")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--pair", default="BTC/USDT")
    parser.add_argument("--max-ticker-staleness-seconds", type=float, default=30)
    parser.add_argument("--max-orderbook-staleness-seconds", type=float, default=30)
    parser.add_argument("--max-spread-bps", type=float, default=10)
    parser.add_argument("--min-orderbook-levels", type=int, default=5)
    parser.add_argument("--max-clock-skew-seconds", type=float, default=5)
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/live-market"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    report_path = run_live_market_check(
        output_dir=args.output_dir,
        exchange=args.exchange,
        pair=args.pair,
        policy=LiveMarketPolicy(
            max_ticker_staleness_seconds=args.max_ticker_staleness_seconds,
            max_orderbook_staleness_seconds=args.max_orderbook_staleness_seconds,
            max_spread_bps=args.max_spread_bps,
            min_orderbook_levels=args.min_orderbook_levels,
            max_clock_skew_seconds=args.max_clock_skew_seconds,
        ),
        root_dir=args.root_dir.resolve(),
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"]["passed"] else 1


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _levels(value: object) -> list[tuple[float, float]]:
    if not isinstance(value, list):
        return []

    levels = []
    for level in value:
        if not isinstance(level, list | tuple) or len(level) < 2:
            continue
        price = _finite_float(level[0])
        amount = _finite_number(level[1])
        if price is not None and amount is not None:
            levels.append((price, amount))
    return levels


def _finite_float(value: object) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _spread_bps(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    midpoint = (bid + ask) / 2
    if midpoint <= 0:
        return None
    return round(((ask - bid) / midpoint) * 10_000, 2)


def _staleness_seconds(timestamp: object, *, captured_at: datetime) -> float | None:
    if timestamp is None:
        return None
    try:
        timestamp_value = float(timestamp)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp_value) or timestamp_value <= 0:
        return None

    if timestamp_value > 10_000_000_000:
        timestamp_value = timestamp_value / 1000
    event_time = datetime.fromtimestamp(timestamp_value, tz=timezone.utc)
    return round((_normalize_datetime(captured_at) - event_time).total_seconds(), 2)


def _normalize_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

