# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

from qt.legacy.btc_quant_evolution.features.feature_matrix import validate_strategy_schema
from qt.legacy.btc_quant_evolution.ohlcv_integrity import data_file_path, timeframe_to_seconds

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def validate_feature_matrix_window(
    *,
    matrix_path: Path,
    ohlcv_path: Path,
    timeframe: str,
    timerange: str,
    require_exact_matrix_window: bool = True,
) -> dict[str, object]:
    start, end = _parse_timerange(timerange)
    interval = pd.Timedelta(seconds=timeframe_to_seconds(timeframe))
    expected_dates = pd.date_range(start, end, freq=interval, inclusive="left")

    matrix = pd.read_parquet(matrix_path).copy()
    _require_columns(matrix, {"date", *OHLCV_COLUMNS})
    matrix["date"] = pd.to_datetime(matrix["date"], utc=True, errors="raise").dt.as_unit("ns")
    matrix_window = matrix.loc[(matrix["date"] >= start) & (matrix["date"] < end)].sort_values("date")
    if require_exact_matrix_window and len(matrix) != len(matrix_window):
        raise ValueError("feature matrix must contain exactly the requested timerange")
    _require_exact_dates(matrix_window, expected_dates, label="matrix")
    validate_strategy_schema(matrix_window)
    _validate_source_availability(matrix_window)

    ohlcv = pd.read_feather(ohlcv_path).copy()
    _require_columns(ohlcv, {"date", *OHLCV_COLUMNS})
    ohlcv["date"] = pd.to_datetime(ohlcv["date"], utc=True, errors="raise").dt.as_unit("ns")
    ohlcv_window = ohlcv.loc[(ohlcv["date"] >= start) & (ohlcv["date"] < end)].sort_values("date")
    _require_exact_dates(ohlcv_window, expected_dates, label="OHLCV")

    try:
        assert_frame_equal(
            matrix_window.loc[:, ["date", *OHLCV_COLUMNS]].reset_index(drop=True),
            ohlcv_window.loc[:, ["date", *OHLCV_COLUMNS]].reset_index(drop=True),
            check_dtype=False,
            check_exact=True,
        )
    except AssertionError as exc:
        raise ValueError("feature matrix embedded OHLCV does not match downloaded OHLCV") from exc

    return {
        "end": end.isoformat().replace("+00:00", "Z"),
        "passed": True,
        "row_count": len(matrix_window),
        "start": start.isoformat().replace("+00:00", "Z"),
        "timeframe": timeframe,
    }


def _validate_source_availability(matrix: pd.DataFrame) -> None:
    columns = sorted(column for column in matrix if column.endswith("_available_at"))
    if not columns:
        raise ValueError("feature matrix has no source availability columns")
    for column in columns:
        availability = pd.to_datetime(matrix[column], utc=True, errors="coerce")
        observed = availability.notna()
        if not observed.any() or (availability[observed] > matrix.loc[observed, "date"]).any():
            raise ValueError(f"feature matrix violates source availability contract: {column}")


def _require_exact_dates(frame: pd.DataFrame, expected: pd.DatetimeIndex, *, label: str) -> None:
    observed = pd.DatetimeIndex(frame["date"])
    if observed.has_duplicates or not observed.equals(expected):
        raise ValueError(f"{label} dates do not exactly cover requested timerange")


def _require_columns(frame: pd.DataFrame, required: set[str]) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"feature matrix quality input missing columns: {', '.join(missing)}")


def _parse_timerange(timerange: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    try:
        start_text, end_text = timerange.split("-", maxsplit=1)
        start = pd.to_datetime(start_text, format="%Y%m%d", exact=True, utc=True)
        end = pd.to_datetime(end_text, format="%Y%m%d", exact=True, utc=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("timerange must use YYYYMMDD-YYYYMMDD") from exc
    if start >= end:
        raise ValueError("timerange start must be before end")
    return start, end


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate feature-matrix coverage and OHLCV identity.")
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--pair", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--timerange", required=True)
    parser.add_argument("--allow-matrix-superset", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    root_dir = args.root_dir.resolve()
    matrix_path = args.matrix if args.matrix.is_absolute() else root_dir / args.matrix
    ohlcv_path = data_file_path(
        root_dir=root_dir,
        exchange=args.exchange,
        pair=args.pair,
        timeframe=args.timeframe,
    )
    try:
        result = validate_feature_matrix_window(
            matrix_path=matrix_path,
            ohlcv_path=ohlcv_path,
            timeframe=args.timeframe,
            timerange=args.timerange,
            require_exact_matrix_window=not args.allow_matrix_superset,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        result = {"failed_reasons": [str(exc)], "passed": False}

    output_dir = args.output_dir if args.output_dir.is_absolute() else root_dir / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "feature_matrix_quality.json"
    report_path.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())

