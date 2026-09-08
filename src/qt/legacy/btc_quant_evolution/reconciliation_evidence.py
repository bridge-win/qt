# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_evolution.ops.reconciliation import OpenTrade, reconcile_spot_position


DEFAULT_TOLERANCE = 0.000_001


def evaluate_reconciliation_evidence(
    *,
    pair: str,
    open_trades: list[dict[str, Any]],
    exchange_free_amount: float,
    exchange_used_amount: float,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    asset = _asset_from_pair(pair)
    parsed_trades = [_open_trade_from_mapping(trade) for trade in open_trades]
    result = reconcile_spot_position(
        pair=pair,
        open_trades=parsed_trades,
        exchange_free_amount=exchange_free_amount,
        exchange_used_amount=exchange_used_amount,
        tolerance=tolerance,
    )
    failed_reasons = []
    if not result.within_tolerance:
        failed_reasons.append(f"{pair} exchange amount differs from expected open-trade amount")

    return {
        "asset": asset,
        "difference": _round_amount(result.difference),
        "exchange_amount": _round_amount(result.exchange_amount),
        "exchange_free_amount": _round_amount(exchange_free_amount),
        "exchange_used_amount": _round_amount(exchange_used_amount),
        "expected_amount": _round_amount(result.expected_amount),
        "failed_reasons": failed_reasons,
        "open_trade_count": sum(1 for trade in parsed_trades if trade.pair == pair),
        "pair": pair,
        "passed": not failed_reasons,
        "tolerance": tolerance,
    }


def write_reconciliation_evidence(
    *,
    output_dir: Path,
    root_dir: Path,
    pair: str,
    open_trades_path: Path,
    balance_path: Path,
    tolerance: float = DEFAULT_TOLERANCE,
) -> Path:
    try:
        open_trades = _parse_open_trades(json.loads(open_trades_path.read_text()))
        free_amount, used_amount = _parse_balance(
            payload=json.loads(balance_path.read_text()),
            asset=_asset_from_pair(pair),
        )
        report = evaluate_reconciliation_evidence(
            pair=pair,
            open_trades=open_trades,
            exchange_free_amount=free_amount,
            exchange_used_amount=used_amount,
            tolerance=tolerance,
        )
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError) as exc:
        report = {
            "asset": _asset_from_pair(pair),
            "failed_reasons": [f"reconciliation input error: {exc}"],
            "pair": pair,
            "passed": False,
            "tolerance": tolerance,
        }

    report["open_trades_path"] = _relative_path(path=open_trades_path, root_dir=root_dir)
    report["balance_path"] = _relative_path(path=balance_path, root_dir=root_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "reconciliation.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Write exchange/open-trade reconciliation evidence.")
    parser.add_argument("--pair", required=True)
    parser.add_argument("--open-trades", type=Path, required=True)
    parser.add_argument("--balance", type=Path, required=True)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/live-evidence"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    report_path = write_reconciliation_evidence(
        output_dir=args.output_dir,
        root_dir=args.root_dir.resolve(),
        pair=args.pair,
        open_trades_path=args.open_trades,
        balance_path=args.balance,
        tolerance=args.tolerance,
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] is True else 1


def _parse_open_trades(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("open_trades")
    if not isinstance(payload, list):
        raise ValueError("open trades snapshot must be a list or contain open_trades list")
    trades = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("open trade entries must be JSON objects")
        trades.append(item)
    return trades


def _parse_balance(*, payload: Any, asset: str) -> tuple[float, float]:
    if not isinstance(payload, dict):
        raise ValueError("balance snapshot must be a JSON object")

    direct_asset_balance = payload.get(asset)
    if isinstance(direct_asset_balance, dict):
        return _numeric_field(direct_asset_balance, "free"), _numeric_field(direct_asset_balance, "used")

    free = payload.get("free")
    used = payload.get("used")
    if isinstance(free, dict) and isinstance(used, dict):
        return _numeric_value(free.get(asset), f"free.{asset}"), _numeric_value(used.get(asset), f"used.{asset}")

    raise ValueError(f"balance snapshot missing {asset} free/used amounts")


def _open_trade_from_mapping(trade: dict[str, Any]) -> OpenTrade:
    pair = trade.get("pair")
    if not isinstance(pair, str) or not pair:
        raise ValueError("open trade missing pair")
    return OpenTrade(pair=pair, amount=_numeric_value(trade.get("amount"), "amount"))


def _numeric_field(payload: dict[str, Any], field: str) -> float:
    return _numeric_value(payload.get(field), field)


def _numeric_value(value: Any, name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be numeric")
    if isinstance(value, int | float | str):
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be numeric") from exc
    raise ValueError(f"{name} must be numeric")


def _asset_from_pair(pair: str) -> str:
    if "/" not in pair:
        raise ValueError("pair must use BASE/QUOTE format")
    asset = pair.split("/", maxsplit=1)[0]
    if not asset:
        raise ValueError("pair must include a base asset")
    return asset


def _round_amount(value: float) -> float:
    return round(value, 12)


def _relative_path(*, path: Path, root_dir: Path) -> str:
    try:
        return path.resolve().relative_to(root_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
