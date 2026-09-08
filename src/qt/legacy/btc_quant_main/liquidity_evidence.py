# Migrated verbatim from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LiquidityPolicy:
    order_notional: float
    max_impact_bps: float = 20.0
    min_depth_multiple: float = 2.0

    def __post_init__(self) -> None:
        if self.order_notional <= 0:
            raise ValueError("order_notional must be positive")
        if self.max_impact_bps < 0:
            raise ValueError("max_impact_bps cannot be negative")
        if self.min_depth_multiple < 0:
            raise ValueError("min_depth_multiple cannot be negative")


def evaluate_orderbook_liquidity(
    *,
    snapshot: dict[str, Any],
    policy: LiquidityPolicy,
) -> dict[str, Any]:
    orderbook = snapshot.get("orderbook") if isinstance(snapshot.get("orderbook"), dict) else {}
    asks = _levels(orderbook.get("asks"))
    bids = _levels(orderbook.get("bids"))
    available_buy_notional = _notional_depth(asks)
    available_sell_notional = _notional_depth(bids)
    buy_depth_multiple = _ratio(available_buy_notional, policy.order_notional)
    sell_depth_multiple = _ratio(available_sell_notional, policy.order_notional)
    buy_impact_bps = _buy_impact_bps(asks, order_notional=policy.order_notional)
    sell_impact_bps = _sell_impact_bps(bids, order_notional=policy.order_notional)
    failed_reasons = []

    if buy_depth_multiple < policy.min_depth_multiple:
        failed_reasons.append(f"buy depth below {policy.min_depth_multiple:g}x order notional")
    if sell_depth_multiple < policy.min_depth_multiple:
        failed_reasons.append(f"sell depth below {policy.min_depth_multiple:g}x order notional")
    if buy_impact_bps is None:
        failed_reasons.append("buy side cannot fill order notional")
    elif buy_impact_bps > policy.max_impact_bps:
        failed_reasons.append(f"buy impact above {policy.max_impact_bps:g} bps")
    if sell_impact_bps is None:
        failed_reasons.append("sell side cannot fill order notional")
    elif sell_impact_bps > policy.max_impact_bps:
        failed_reasons.append(f"sell impact above {policy.max_impact_bps:g} bps")

    return {
        "available_buy_notional": _round(available_buy_notional),
        "available_sell_notional": _round(available_sell_notional),
        "buy_depth_multiple": _round(buy_depth_multiple),
        "buy_impact_bps": _round(buy_impact_bps) if buy_impact_bps is not None else None,
        "failed_reasons": failed_reasons,
        "order_notional": policy.order_notional,
        "passed": not failed_reasons,
        "sell_depth_multiple": _round(sell_depth_multiple),
        "sell_impact_bps": _round(sell_impact_bps) if sell_impact_bps is not None else None,
    }


def write_liquidity_evidence(
    *,
    output_dir: Path,
    root_dir: Path,
    live_market_path: Path,
    policy: LiquidityPolicy,
) -> Path:
    live_market_report = json.loads(live_market_path.read_text())
    snapshot = live_market_report.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    report = {
        **evaluate_orderbook_liquidity(snapshot=snapshot, policy=policy),
        "live_market_path": _relative_path(path=live_market_path, root_dir=root_dir),
        "live_market_result_passed": _live_market_passed(live_market_report),
        "policy": asdict(policy),
    }
    if report["live_market_result_passed"] is not True:
        report["passed"] = False
        report["failed_reasons"] = [
            *report["failed_reasons"],
            "live_market result did not pass",
        ]

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "liquidity.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Write live order-book liquidity evidence.")
    parser.add_argument("--live-market", type=Path, required=True)
    parser.add_argument("--order-notional", type=float, required=True)
    parser.add_argument("--max-impact-bps", type=float, default=20.0)
    parser.add_argument("--min-depth-multiple", type=float, default=2.0)
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/live-evidence"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    report_path = write_liquidity_evidence(
        output_dir=args.output_dir,
        root_dir=args.root_dir.resolve(),
        live_market_path=args.live_market,
        policy=LiquidityPolicy(
            order_notional=args.order_notional,
            max_impact_bps=args.max_impact_bps,
            min_depth_multiple=args.min_depth_multiple,
        ),
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] is True else 1


def _buy_impact_bps(levels: list[tuple[float, float]], *, order_notional: float) -> float | None:
    if not levels:
        return None
    best_ask = levels[0][0]
    remaining_notional = order_notional
    acquired_base = 0.0
    for price, amount in levels:
        level_notional = price * amount
        consumed_notional = min(remaining_notional, level_notional)
        acquired_base += consumed_notional / price
        remaining_notional -= consumed_notional
        if remaining_notional <= 1e-12:
            break
    if remaining_notional > 1e-12 or acquired_base <= 0:
        return None
    average_price = order_notional / acquired_base
    return ((average_price - best_ask) / best_ask) * 10_000


def _sell_impact_bps(levels: list[tuple[float, float]], *, order_notional: float) -> float | None:
    if not levels:
        return None
    best_bid = levels[0][0]
    target_base = order_notional / best_bid
    remaining_base = target_base
    proceeds = 0.0
    for price, amount in levels:
        consumed_base = min(remaining_base, amount)
        proceeds += consumed_base * price
        remaining_base -= consumed_base
        if remaining_base <= 1e-12:
            break
    if remaining_base > 1e-12 or target_base <= 0:
        return None
    average_price = proceeds / target_base
    return ((best_bid - average_price) / best_bid) * 10_000


def _levels(value: object) -> list[tuple[float, float]]:
    if not isinstance(value, list):
        return []
    levels = []
    for level in value:
        if not isinstance(level, list | tuple) or len(level) < 2:
            continue
        price = _positive_float(level[0])
        amount = _positive_float(level[1])
        if price is not None and amount is not None:
            levels.append((price, amount))
    return levels


def _notional_depth(levels: list[tuple[float, float]]) -> float:
    return sum(price * amount for price, amount in levels)


def _positive_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _live_market_passed(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    result = payload.get("result")
    return isinstance(result, dict) and result.get("passed") is True


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def _round(value: float) -> float:
    return round(value, 4)


def _relative_path(*, path: Path, root_dir: Path) -> str:
    try:
        return path.resolve().relative_to(root_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())

