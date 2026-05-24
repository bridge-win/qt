"""Backtest artifact export for dashboards and later analysis."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from qt.backtest.engine import BacktestResult


@dataclass(frozen=True)
class BacktestArtifact:
    run_id: str
    run_dir: Path
    summary_path: Path
    equity_path: Path
    trades_path: Path
    signals_path: Path


def write_backtest_artifacts(
    result: BacktestResult,
    output_dir: str | Path,
    *,
    ohlcv_key: str,
    initial_cash: float,
    config_path: str | Path | None = None,
    sources: Mapping[str, str | None] | None = None,
) -> BacktestArtifact:
    """Write summary JSON and CSV detail files for a backtest run."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / run_id
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = root / f"{run_id}-{suffix}"
    run_dir.mkdir(parents=True)

    equity_path = run_dir / "equity.csv"
    trades_path = run_dir / "trades.csv"
    signals_path = run_dir / "signals.csv"
    summary_path = run_dir / "summary.json"

    result.equity_curve.to_frame().to_csv(equity_path, index_label="ts")
    result.trades.to_csv(trades_path, index=False)
    result.signals.to_csv(signals_path, index=False)

    summary = {
        "run_id": run_dir.name,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "ohlcv_key": ohlcv_key,
        "initial_cash": initial_cash,
        "config_path": str(config_path) if config_path else None,
        "sources": dict(sources or {}),
        "metrics": asdict(result.metrics),
        "equity": {
            "start": _series_ts(result.equity_curve, first=True),
            "end": _series_ts(result.equity_curve, first=False),
            "start_value": _series_value(result.equity_curve, first=True),
            "end_value": _series_value(result.equity_curve, first=False),
        },
        "counts": {
            "equity_points": len(result.equity_curve),
            "trades": len(result.trades),
            "signals": len(result.signals),
        },
        "files": {
            "equity": str(equity_path),
            "trades": str(trades_path),
            "signals": str(signals_path),
        },
    }
    _write_json(summary_path, summary)
    _write_json(root / "latest.json", summary)
    return BacktestArtifact(
        run_id=run_dir.name,
        run_dir=run_dir,
        summary_path=summary_path,
        equity_path=equity_path,
        trades_path=trades_path,
        signals_path=signals_path,
    )


def latest_backtest_summary(output_dir: str | Path) -> dict[str, object] | None:
    """Load the most recent exported backtest summary, if one exists."""

    latest_path = Path(output_dir) / "latest.json"
    if latest_path.exists():
        return _read_json(latest_path)

    summaries = sorted(Path(output_dir).glob("*/summary.json"), key=lambda p: p.stat().st_mtime)
    if not summaries:
        return None
    return _read_json(summaries[-1])


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(_json_safe(payload), fh, indent=2, sort_keys=True, allow_nan=False)
        fh.write("\n")


def _read_json(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return {}
    return data


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    return value


def _series_ts(series: pd.Series, *, first: bool) -> str | None:
    if series.empty:
        return None
    ts = series.index[0] if first else series.index[-1]
    if hasattr(ts, "isoformat"):
        return str(ts.isoformat())
    return str(ts)


def _series_value(series: pd.Series, *, first: bool) -> float | None:
    if series.empty:
        return None
    value = float(series.iloc[0] if first else series.iloc[-1])
    return value if math.isfinite(value) else None
