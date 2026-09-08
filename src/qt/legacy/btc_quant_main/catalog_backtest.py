# Migrated with Python 3.10 UTC compatibility from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qt.legacy.btc_quant_main.research_pipeline import BacktestRequest, run_research_plan
from qt.legacy.btc_quant_main.strategy_catalog import (
    build_catalog,
    profile_by_id,
    write_runtime_catalog,
)


@dataclass(frozen=True)
class CatalogBatchResult:
    batch_dir: Path
    exit_code: int
    result_count: int
    failed_count: int


def validate_catalog_runtime(*, root_dir: Path):
    return write_runtime_catalog(root_dir=root_dir)


def list_profiles() -> list[dict[str, object]]:
    catalog = build_catalog()
    return [
        {
            "id": profile.id,
            "family": profile.family,
            "version": profile.version,
            "entry_signals": list(profile.entry_signals),
            "exit_signals": list(profile.exit_signals),
            "risk": profile.risk,
        }
        for profile in catalog.profiles
    ]


def run_catalog_profile(
    *,
    root_dir: Path,
    profile_id: str,
    timerange: str | None,
    days: int | None,
    run_id: str,
    exchange: str,
    pair: str,
    timeframe: str,
    include_bias_checks: bool,
    exchange_check: bool,
    fee_bps_per_side: float = 0.0,
    slippage_bps_per_side: float = 10.0,
) -> dict[str, object]:
    catalog = build_catalog()
    profile = profile_by_id(catalog, profile_id)
    if profile.pair != pair or profile.timeframe != timeframe:
        raise ValueError(f"profile {profile_id} only supports {profile.pair} {profile.timeframe}")

    validate_catalog_runtime(root_dir=root_dir)
    request = BacktestRequest(
        exchange=exchange,
        pair=pair,
        timeframe=timeframe,
        days=days,
        timerange=timerange,
        strategy="BtcCatalogStrategy",
        run_id=run_id,
        include_bias_checks=include_bias_checks,
        exchange_check=exchange_check,
        profile=profile_id,
        fee_bps_per_side=fee_bps_per_side,
        slippage_bps_per_side=slippage_bps_per_side,
    )
    run_dir = run_research_plan(request=request, root_dir=root_dir)
    return _completed_result(profile_id=profile_id, run_dir=run_dir)


def run_catalog_batch(
    *,
    root_dir: Path,
    timerange: str | None,
    run_id: str,
    profiles: list[str] | None = None,
    days: int | None = None,
    exchange: str = "binance",
    pair: str = "BTC/USDT",
    timeframe: str = "4h",
    include_bias_checks: bool = True,
    exchange_check: bool = True,
    fee_bps_per_side: float = 0.0,
    slippage_bps_per_side: float = 10.0,
) -> CatalogBatchResult:
    validate_catalog_runtime(root_dir=root_dir)
    catalog = build_catalog()
    profile_ids = profiles or [profile.id for profile in catalog.profiles]
    batch_dir = root_dir / "user_data" / "catalog_batches" / run_id
    if batch_dir.exists():
        raise ValueError(f"catalog batch output already exists: {batch_dir}")
    batch_dir.mkdir(parents=True)

    results: list[dict[str, object]] = []
    for profile_id in profile_ids:
        child_run_id = f"{run_id}-{profile_id}"
        try:
            result = run_catalog_profile(
                root_dir=root_dir,
                profile_id=profile_id,
                timerange=timerange,
                days=days,
                run_id=child_run_id,
                exchange=exchange,
                pair=pair,
                timeframe=timeframe,
                include_bias_checks=include_bias_checks,
                exchange_check=exchange_check,
                fee_bps_per_side=fee_bps_per_side,
                slippage_bps_per_side=slippage_bps_per_side,
            )
        except Exception as exc:
            result = {
                "profile": profile_id,
                "status": "failed",
                "run_dir": str(root_dir / "user_data" / "research_runs" / child_run_id),
                "error": str(exc),
            }
        results.append(result)

    artifact = validate_catalog_runtime(root_dir=root_dir)
    results_payload = {
        "run_id": run_id,
        "created_at": _utc_now(),
        "request": {
            "exchange": exchange,
            "pair": pair,
            "timeframe": timeframe,
            "timerange": timerange,
            "days": days,
            "include_bias_checks": include_bias_checks,
            "exchange_check": exchange_check,
        },
        "catalog_sha256": artifact.sha256,
        "results": results,
    }
    (batch_dir / "catalog_results.json").write_text(json.dumps(results_payload, indent=2, sort_keys=True) + "\n")
    ranking = rank_catalog_results(results)
    (batch_dir / "catalog_ranking.json").write_text(json.dumps(ranking, indent=2, sort_keys=True) + "\n")
    failed_count = sum(1 for result in results if result.get("status") == "failed")
    return CatalogBatchResult(
        batch_dir=batch_dir,
        exit_code=1 if failed_count else 0,
        result_count=len(results),
        failed_count=failed_count,
    )


def rank_catalog_results(results: list[dict[str, object]]) -> list[dict[str, object]]:
    ranked = []
    for result in results:
        if result.get("status") != "completed":
            continue
        summary = result.get("summary")
        readiness = result.get("readiness")
        if not isinstance(summary, dict) or not isinstance(readiness, dict):
            continue
        metrics = _ranking_metrics(summary)
        if metrics is None:
            continue
        ranked.append(
            {
                "profile": result["profile"],
                "readiness_passed": readiness.get("passed") is True,
                **metrics,
            }
        )

    return sorted(
        ranked,
        key=lambda item: (
            not item["readiness_passed"],
            -float(item["calmar"]),
            -float(item["profit_factor"]),
            -float(item["sortino"]),
            -int(item["total_trades"]),
            str(item["profile"]),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and backtest BTC catalog profiles.")
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list")
    subparsers.add_parser("validate")

    run_parser = subparsers.add_parser("run")
    run_group = run_parser.add_mutually_exclusive_group(required=True)
    run_group.add_argument("--profile")
    run_group.add_argument("--all", action="store_true")
    run_window = run_parser.add_mutually_exclusive_group(required=True)
    run_window.add_argument("--timerange")
    run_window.add_argument("--days", type=int)
    run_parser.add_argument("--run-id", default=_default_run_id())
    run_parser.add_argument("--exchange", default="binance")
    run_parser.add_argument("--pair", default="BTC/USDT")
    run_parser.add_argument("--timeframe", default="4h")
    run_parser.add_argument("--skip-bias-checks", action="store_true")
    run_parser.add_argument("--no-exchange-check", action="store_true")
    run_parser.add_argument("--fee-bps-per-side", type=float, default=0.0)
    run_parser.add_argument("--slippage-bps-per-side", type=float, default=10.0)

    args = parser.parse_args()
    root_dir = args.root_dir.resolve()

    if args.command == "list":
        print(json.dumps(list_profiles(), indent=2, sort_keys=True))
        return 0
    if args.command == "validate":
        artifact = validate_catalog_runtime(root_dir=root_dir)
        print(f"catalog={artifact.path} sha256={artifact.sha256}")
        return 0

    if args.all:
        result = run_catalog_batch(
            root_dir=root_dir,
            timerange=args.timerange,
            days=args.days,
            run_id=args.run_id,
            exchange=args.exchange,
            pair=args.pair,
            timeframe=args.timeframe,
            include_bias_checks=not args.skip_bias_checks,
            exchange_check=not args.no_exchange_check,
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
        )
        print(f"catalog_batch={result.batch_dir} failed={result.failed_count}")
        return result.exit_code

    profile_result = run_catalog_profile(
        root_dir=root_dir,
        profile_id=args.profile,
        timerange=args.timerange,
        days=args.days,
        run_id=args.run_id,
        exchange=args.exchange,
        pair=args.pair,
        timeframe=args.timeframe,
        include_bias_checks=not args.skip_bias_checks,
        exchange_check=not args.no_exchange_check,
        fee_bps_per_side=args.fee_bps_per_side,
        slippage_bps_per_side=args.slippage_bps_per_side,
    )
    print(f"catalog_profile={profile_result['profile']} run_dir={profile_result['run_dir']}")
    return 0


def _completed_result(*, profile_id: str, run_dir: Path) -> dict[str, object]:
    summary = _read_json_if_exists(run_dir / "summary.json")
    readiness = _read_json_if_exists(run_dir / "readiness.json")
    return {
        "profile": profile_id,
        "status": "completed",
        "run_dir": str(run_dir),
        "summary": summary,
        "readiness": readiness,
    }


def _read_json_if_exists(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact is not an object: {path}")
    return payload


def _ranking_metrics(summary: dict[str, object]) -> dict[str, float | int] | None:
    profit_factor = _number_or_none(summary.get("profit_factor"))
    sortino = _number_or_none(summary.get("sortino"))
    total_trades = _number_or_none(summary.get("total_trades"))
    calmar = _number_or_none(summary.get("calmar"))
    if calmar is None:
        cagr_pct = _number_or_none(summary.get("cagr_pct"))
        drawdown_pct = _number_or_none(summary.get("max_drawdown_pct"))
        if cagr_pct is not None and drawdown_pct is not None and drawdown_pct > 0:
            calmar = cagr_pct / drawdown_pct
    if profit_factor is None or sortino is None or total_trades is None or calmar is None:
        return None
    return {
        "calmar": float(calmar),
        "profit_factor": float(profit_factor),
        "sortino": float(sortino),
        "total_trades": int(total_trades),
    }


def _number_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _default_run_id() -> str:
    return f"catalog-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

