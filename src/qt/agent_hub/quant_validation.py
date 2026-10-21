"""Bounded, independently testable validation of native research evidence.

These checks attest observations, not profitability. Resampling conditions on the
submitted candidate and observed folds; it never corrects undisclosed searches.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

_DAY = timedelta(days=1)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("native timestamp must be ISO-8601 text")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("native timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


def audit_native_timeline(
    result: Mapping[str, Any],
    *,
    start: str,
    end: str,
    snapshot_start_ms: int,
) -> dict[str, Any]:
    """Verify actual native observations, independently of declared window metadata.

    Input bars identify UTC opens; decisions and PnL observations identify closes.
    Native equity includes one initial cash observation before the decision window
    for warmup. That observation must not inflate annualization duration.
    """
    first, last = _timestamp(start), _timestamp(end)
    snapshot_start = datetime.fromtimestamp(snapshot_start_ms / 1000, tz=timezone.utc)
    count = int((last - first) / _DAY) + 1
    if count < 1 or (last - first) % _DAY:
        raise ValueError("native execution window is not aligned to daily bars")
    expected = [first + _DAY * (index + 1) for index in range(count)]
    returns = result.get("series", {}).get("returns")
    traces = result.get("decision_traces")
    equity = result.get("series", {}).get("equity")
    if not isinstance(returns, dict) or not isinstance(traces, list) or not isinstance(equity, dict):
        raise ValueError("native evidence lacks returns, equity or decision traces")
    if [_timestamp(value) for value in returns] != expected:
        raise ValueError("native return timestamps do not exactly cover the closed-bar window")
    if [_timestamp(trace.get("timestamp")) for trace in traces] != expected:
        raise ValueError("native decision timestamps do not exactly cover the closed-bar window")
    if [_timestamp(value) for value in equity] != [snapshot_start, *expected]:
        raise ValueError("native equity timestamps do not match initial cash and closed bars")
    values = [float(value) for value in equity.values()]
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("native portfolio equity must be finite and positive")
    for index, (timestamp, trace) in enumerate(zip(expected, traces, strict=True)):
        # A close at T may observe exactly the bars opening before T, not T's bar.
        visible = int((timestamp - snapshot_start) / _DAY)
        if type(trace.get("observed_rows")) is not int or trace["observed_rows"] != visible:
            raise ValueError("native decision observed_rows reveals missing or future bars")
        value = float(list(returns.values())[index])
        reconstructed = values[index + 1] / values[index] - 1
        if not math.isfinite(value) or not math.isclose(value, reconstructed, abs_tol=1e-12):
            raise ValueError("native return does not reconcile to portfolio equity")
    return {
        "status": "verified",
        "bars": count,
        "first_close": expected[0].isoformat(),
        "last_close": expected[-1].isoformat(),
        "decision_coverage": "all_closed_bars_exactly_once",
        "observed_rows": "exact_causal_prefix",
        "returns": "reconciled_to_native_equity",
        "annualization": "decision_window_only_excludes_warmup",
    }


def annualized_return(total_return: float, bars: int) -> float | None:
    """Calendar-day annualization, with no warmup or fictitious flat observations."""
    if bars < 1 or total_return < -1 or not math.isfinite(total_return):
        raise ValueError("invalid annualization inputs")
    if total_return == -1:
        return -1.0
    exponent = math.log1p(total_return) * 365.25 / bars
    return math.expm1(exponent) if exponent < 700 else None


def fold_bootstrap(
    folds: Sequence[Sequence[float]],
    *,
    block_sizes: Sequence[int] = (7, 14, 28),
    simulations: int = 1000,
    seed: int = 7,
) -> dict[str, Any]:
    """Circular block sensitivity analysis without crossing portfolio-reset folds.

    Keep each fold's sample size and chronology in the stitched experiment. Block
    starts may wrap within a fold, but may never manufacture dependence across
    independently reset portfolios. Percentiles are conditional diagnostics, not
    a multiple-testing correction or a probability of future investment success.
    """
    if not folds or any(not fold for fold in folds):
        raise ValueError("bootstrap requires nonempty OOS folds")
    if any(not math.isfinite(value) or value <= -1 for fold in folds for value in fold):
        raise ValueError("bootstrap requires finite returns greater than -1")
    if simulations < 100 or simulations > 5000 or not block_sizes:
        raise ValueError("bootstrap simulation budget must be within [100, 5000]")
    if any(size < 1 or size > 365 for size in block_sizes):
        raise ValueError("bootstrap block size must be within [1, 365]")
    sensitivities = []
    for block_size in block_sizes:
        rng = random.Random(seed + block_size)
        finals, drawdowns = [], []
        for _ in range(simulations):
            equity, peak, drawdown = 1.0, 1.0, 0.0
            for fold in folds:
                sampled = 0
                while sampled < len(fold):
                    start = rng.randrange(len(fold))
                    for offset in range(min(block_size, len(fold) - sampled)):
                        equity *= 1 + fold[(start + offset) % len(fold)]
                        peak = max(peak, equity)
                        drawdown = min(drawdown, equity / peak - 1)
                        sampled += 1
            finals.append(equity - 1)
            drawdowns.append(drawdown)
        finals.sort()
        drawdowns.sort()
        sensitivities.append(
            {
                "block_size": block_size,
                "loss_probability": sum(value <= 0 for value in finals) / simulations,
                "return_p05": _quantile(finals, 0.05),
                "return_p50": _quantile(finals, 0.5),
                "return_p95": _quantile(finals, 0.95),
                "max_drawdown_p05": _quantile(drawdowns, 0.05),
            }
        )
    return {
        "method": "circular_blocks_within_each_oos_fold",
        "simulations": simulations,
        "seed": seed,
        "fold_bars": [len(fold) for fold in folds],
        "cross_fold_blocks": False,
        "sensitivity": sensitivities,
        "worst_return_p05": min(row["return_p05"] for row in sensitivities),
        "worst_loss_probability": max(row["loss_probability"] for row in sensitivities),
        "scope": "fixed candidate conditional on observed folds; not search-adjusted",
    }


def _quantile(values: list[float], probability: float) -> float:
    position = probability * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)
