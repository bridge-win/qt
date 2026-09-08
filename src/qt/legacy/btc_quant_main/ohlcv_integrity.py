# Migrated with Python 3.10 UTC compatibility from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PRICE_COLUMNS = ("open", "high", "low", "close")


def timeframe_to_seconds(timeframe: str) -> int:
    if len(timeframe) < 2:
        raise ValueError(f"unsupported timeframe: {timeframe}")

    value = int(timeframe[:-1])
    unit = timeframe[-1]
    multipliers = {"m": 60, "h": 3600, "d": 86_400}
    if value <= 0 or unit not in multipliers:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return value * multipliers[unit]


def data_file_path(*, root_dir: Path, exchange: str, pair: str, timeframe: str) -> Path:
    pair_file = pair.replace("/", "_").replace(":", "_")
    return root_dir / "user_data" / "data" / exchange / f"{pair_file}-{timeframe}.feather"


def read_ohlcv_rows(path: Path) -> list[dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas and pyarrow are required to read Freqtrade feather data") from exc

    dataframe = pd.read_feather(path)
    return list(dataframe.to_dict(orient="records"))


def write_ohlcv_integrity_report(
    *,
    output_dir: Path,
    exchange: str,
    pair: str,
    timeframe: str,
    rows: Iterable[dict[str, Any]],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "exchange": exchange,
        "pair": pair,
        "result": evaluate_ohlcv_integrity(rows=list(rows), timeframe=timeframe),
        "timeframe": timeframe,
    }
    report_path = output_dir / "ohlcv_integrity.json"
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return report_path


def evaluate_ohlcv_integrity(*, rows: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    expected_step_seconds = timeframe_to_seconds(timeframe)
    timestamps = [_normalize_datetime(row["date"]) for row in rows]
    duplicate_timestamps = _duplicate_timestamps(timestamps)
    gaps = _timestamp_gaps(
        sorted(set(timestamps)),
        expected_step_seconds=expected_step_seconds,
    )
    invalid_candles = _invalid_candles(rows)
    failed_reasons = []

    if not rows:
        failed_reasons.append("no candles found")
    if duplicate_timestamps:
        failed_reasons.append(_count_reason("duplicate timestamp", len(duplicate_timestamps)))
    if gaps:
        failed_reasons.append(_count_reason("timestamp gap", len(gaps)))
    if invalid_candles:
        failed_reasons.append(_count_reason("invalid candle", len(invalid_candles)))

    sorted_timestamps = sorted(timestamps)
    return {
        "candles": len(rows),
        "duplicate_timestamps": [_format_timestamp(timestamp) for timestamp in duplicate_timestamps],
        "end": _format_timestamp(sorted_timestamps[-1]) if sorted_timestamps else None,
        "expected_step_seconds": expected_step_seconds,
        "failed_reasons": failed_reasons,
        "gaps": gaps,
        "invalid_candles": invalid_candles,
        "passed": not failed_reasons,
        "start": _format_timestamp(sorted_timestamps[0]) if sorted_timestamps else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Freqtrade OHLCV file integrity.")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--pair", default="BTC/USDT")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--output-dir", type=Path, default=Path("user_data/research_runs/ohlcv-integrity"))
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()

    root_dir = args.root_dir.resolve()
    input_path = data_file_path(
        root_dir=root_dir,
        exchange=args.exchange,
        pair=args.pair,
        timeframe=args.timeframe,
    )
    rows = read_ohlcv_rows(input_path)
    report_path = write_ohlcv_integrity_report(
        output_dir=args.output_dir,
        exchange=args.exchange,
        pair=args.pair,
        timeframe=args.timeframe,
        rows=rows,
    )
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"]["passed"] else 1


def _duplicate_timestamps(timestamps: list[datetime]) -> list[datetime]:
    seen = set()
    duplicates = []
    for timestamp in timestamps:
        if timestamp in seen and timestamp not in duplicates:
            duplicates.append(timestamp)
        seen.add(timestamp)
    return duplicates


def _timestamp_gaps(
    timestamps: list[datetime],
    *,
    expected_step_seconds: int,
) -> list[dict[str, Any]]:
    gaps = []
    for previous, current in zip(timestamps, timestamps[1:]):
        actual_step_seconds = int((current - previous).total_seconds())
        if actual_step_seconds == expected_step_seconds:
            continue
        if actual_step_seconds > expected_step_seconds:
            missing_candles = (actual_step_seconds // expected_step_seconds) - 1
            gaps.append(
                {
                    "actual_step_seconds": actual_step_seconds,
                    "after": _format_timestamp(previous),
                    "before": _format_timestamp(current),
                    "missing_candles": missing_candles,
                }
            )
    return gaps


def _invalid_candles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    invalid = []
    for index, row in enumerate(rows):
        reasons = _invalid_candle_reasons(row)
        if reasons:
            invalid.append(
                {
                    "index": index,
                    "reasons": reasons,
                    "timestamp": _format_timestamp(_normalize_datetime(row["date"])),
                }
            )
    return invalid


def _invalid_candle_reasons(row: dict[str, Any]) -> list[str]:
    reasons = []
    prices = {column: float(row[column]) for column in PRICE_COLUMNS}
    volume = float(row["volume"])

    for column, value in prices.items():
        if not math.isfinite(value):
            reasons.append(f"non-finite {column}")
    if not math.isfinite(volume):
        reasons.append("non-finite volume")
    if reasons:
        return reasons

    if any(value <= 0 for value in prices.values()):
        reasons.append("non-positive price")
    if prices["high"] < max(prices["open"], prices["close"]):
        reasons.append("high below open or close")
    if prices["low"] > min(prices["open"], prices["close"]):
        reasons.append("low above open or close")
    if prices["low"] > prices["high"]:
        reasons.append("low above high")
    if volume < 0:
        reasons.append("negative volume")
    return reasons


def _normalize_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _count_reason(noun: str, count: int) -> str:
    suffix = "" if count == 1 else "s"
    return f"found {count} {noun}{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())

