"""Net marked-to-market portfolio metrics with explicit undefined values.

Equity includes cash plus marked open positions, after all trading costs. A flow
at time t is assumed to occur at the END of (t-1, t]; subtracting it produces a
time-weighted interval return. Raw trading fills are not an equity curve.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import expm1, isfinite, log, sqrt
from typing import Any

import numpy as np

DAY_NS = 86_400_000_000_000


def _metric(value: float | None, reason: str | None = None, **metadata: Any) -> dict[str, Any]:
    if value is not None and not isfinite(value):
        value, reason = None, "non_finite_result"
    return {"value": value, "reason": reason, **metadata}


def net_equity_metrics(
    equity: Sequence[float],
    timestamp_ns: Sequence[int],
    *,
    external_flows: Sequence[float] | None = None,
    benchmark_daily_returns: Sequence[float] | None = None,
    annual_risk_free_rate: float = 0,
    trade_net_pnl: Sequence[float] | None = None,
    min_tail_samples: int = 30,
    effective_observations: int | None = None,
) -> dict[str, Any]:
    """Return 365-day-calendar metrics; sample limits are visible, not passes.

    Benchmark input must already be aligned to the returned UTC return days.
    Confidence intervals and multiple-testing corrections are separate jobs.
    ``trade_net_pnl`` must contain COMPLETE trade lifecycle P&L including costs.
    """
    values = np.asarray(equity, dtype=float)
    times = np.asarray(timestamp_ns)
    flows = np.zeros(len(values)) if external_flows is None else np.asarray(external_flows, dtype=float)
    if values.ndim != 1 or times.ndim != 1 or flows.ndim != 1:
        raise ValueError("expected_one_dimensional_equity")
    if len(values) < 2 or len(times) != len(values) or len(flows) != len(values):
        raise ValueError("insufficient_or_misaligned_equity")
    if not np.issubdtype(times.dtype, np.integer) or np.any(times < 0) or np.any(np.diff(times) <= 0):
        raise ValueError("timestamps_must_be_strictly_increasing_integer_nanoseconds")
    if not np.all(np.isfinite(values)) or not np.all(np.isfinite(flows)) or np.any(values <= 0):
        raise ValueError("equity_must_be_positive_finite")
    if not isfinite(annual_risk_free_rate) or annual_risk_free_rate <= -1 or min_tail_samples < 1:
        raise ValueError("invalid_metric_configuration")
    interval_returns = (values[1:] - flows[1:]) / values[:-1] - 1
    if np.any(interval_returns <= -1):
        raise ValueError("non_positive_flow_adjusted_equity")
    unit_equity = np.concatenate(([1.0], np.cumprod(1 + interval_returns)))
    daily: dict[int, float] = {}
    for timestamp, ret in zip(times[1:], interval_returns, strict=True):
        day = int(timestamp) // DAY_NS
        daily[day] = daily.get(day, 1.0) * (1 + float(ret))
    daily_returns = np.asarray([daily[day] - 1 for day in sorted(daily)])
    sample_count = len(daily_returns)
    if effective_observations is not None and (
        not isinstance(effective_observations, int)
        or not 1 <= effective_observations <= sample_count
    ):
        raise ValueError("invalid_effective_observations")
    gaps = int(np.sum(np.diff(times) > DAY_NS))
    observations_per_year = 365
    daily_rf = (1 + annual_risk_free_rate) ** (1 / observations_per_year) - 1
    excess = daily_returns - daily_rf
    total = float(unit_equity[-1] - 1)
    years = (int(times[-1]) - int(times[0])) / (365 * DAY_NS)
    log_growth = log(float(unit_equity[-1])) / years
    cagr = expm1(log_growth) if log_growth < 700 else None
    peaks = np.maximum.accumulate(unit_equity)
    drawdowns = 1 - unit_equity / peaks
    mdd = float(np.max(drawdowns))
    peak_time = int(times[0])
    duration = 0.0
    previous_underwater = False
    for timestamp, level, peak in zip(times, unit_equity, peaks, strict=True):
        if level >= peak:
            if previous_underwater:
                duration = max(duration, (int(timestamp) - peak_time) / DAY_NS)
            peak_time = int(timestamp)
        else:
            duration = max(duration, (int(timestamp) - peak_time) / DAY_NS)
        previous_underwater = bool(level < peak)
    sufficient_daily = sample_count >= 2 and gaps == 0
    variance_reason = "insufficient_daily_samples" if sample_count < 2 else "missing_daily_coverage"
    std = float(np.std(excess, ddof=1)) if sufficient_daily else 0.0
    downside = float(np.sqrt(np.mean(np.minimum(excess, 0) ** 2)))
    metrics = {
        "cumulative_return": _metric(total),
        "cagr": _metric(cagr, "overflow" if cagr is None else None, unstable=years < 1),
        "annualized_arithmetic_return": _metric(
            float(np.mean(daily_returns) * 365) if gaps == 0 else None,
            "missing_daily_coverage" if gaps else None,
        ),
        "sharpe": _metric(
            float(np.mean(excess)) / std * sqrt(365) if std else None,
            None if std else (variance_reason if not sufficient_daily else "zero_return_variance"),
        ),
        "sortino": _metric(
            float(np.mean(excess)) / downside * sqrt(365) if sufficient_daily and downside else None,
            None if sufficient_daily and downside else (variance_reason if not sufficient_daily else "no_downside_observations"),
        ),
        "volatility": _metric(std * sqrt(365) if sufficient_daily else None, None if sufficient_daily else variance_reason),
        "max_drawdown": _metric(mdd),
        "drawdown_duration_days": _metric(duration, unrecovered=bool(drawdowns[-1] > 0)),
        "calmar": _metric(cagr / mdd if cagr is not None and mdd else None, "zero_drawdown_or_invalid_cagr" if not mdd or cagr is None else None),
    }
    losses = -daily_returns
    for level in (0.95, 0.99):
        label = int(level * 100)
        threshold = float(np.quantile(losses, level))
        tail = losses[losses >= threshold]
        expected_tail = sample_count * (1 - level)
        effective_tail = None if effective_observations is None else effective_observations * (1 - level)
        reliable = effective_tail is not None and effective_tail >= min_tail_samples and gaps == 0
        metadata = {
            "confidence_level": level,
            "tail_samples": len(tail),
            "expected_tail_samples": expected_tail,
            "effective_tail_samples": effective_tail,
            "reliable_tail_sample": reliable,
            "insufficient_reason": None if reliable else "insufficient_effective_tail_samples_or_coverage",
        }
        metrics[f"var_{label}"] = _metric(threshold, **metadata)
        metrics[f"cvar_{label}"] = _metric(float(np.mean(tail)), **metadata)
    if benchmark_daily_returns is not None:
        baseline = np.asarray(benchmark_daily_returns, dtype=float)
        if baseline.shape != daily_returns.shape or not np.all(np.isfinite(baseline)) or np.any(baseline <= -1):
            raise ValueError("benchmark_must_be_finite_aligned_daily_returns")
        benchmark_variance = float(np.var(baseline, ddof=1)) if sufficient_daily else 0
        beta = float(np.cov(daily_returns, baseline, ddof=1)[0, 1]) / benchmark_variance if benchmark_variance else None
        alpha = (float(np.mean(excess)) - beta * float(np.mean(baseline - daily_rf))) * 365 if beta is not None else None
        metrics["beta"] = _metric(beta, None if beta is not None else "insufficient_or_zero_variance_benchmark")
        metrics["alpha"] = _metric(alpha, None if alpha is not None else "insufficient_or_zero_variance_benchmark", method="point_estimate_no_hac_interval")
        metrics["benchmark_excess_return"] = _metric(total - float(np.prod(1 + baseline) - 1))
    if trade_net_pnl is not None:
        pnl = np.asarray(trade_net_pnl, dtype=float)
        if pnl.ndim != 1 or not np.all(np.isfinite(pnl)):
            raise ValueError("invalid_trade_pnl")
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        metrics["profit_factor"] = _metric(float(np.sum(wins) / -np.sum(losses)) if len(losses) else None, None if len(losses) else "no_losing_trades")
        metrics["expectancy"] = _metric(float(pnl.mean()) if len(pnl) else None, None if len(pnl) else "no_completed_trades")
        metrics["win_rate"] = _metric(float(np.mean(pnl > 0)) if len(pnl) else None, None if len(pnl) else "no_completed_trades")
        metrics["average_net_trade_return"] = _metric(None, "requires_per_trade_capital_denominator")
        metrics["payoff_ratio"] = _metric(float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None, None if len(wins) and len(losses) else "requires_winning_and_losing_trades")
        metrics["max_trade_loss"] = _metric(float(-min(0, float(np.min(pnl)))) if len(pnl) else None, None if len(pnl) else "no_completed_trades")
        longest = streak = 0
        for value in pnl:
            streak = streak + 1 if value < 0 else 0
            longest = max(longest, streak)
        metrics["max_consecutive_losses"] = _metric(float(longest) if len(pnl) else None, None if len(pnl) else "no_completed_trades")
    return {
        "schema_version": 2,
        "calculation_version": "net-mtm-calendar365-v1",
        "calendar": "UTC",
        "periods_per_year": 365,
        "observations": sample_count,
        "effective_observations": effective_observations,
        "effective_observations_reason": "requires_dependence_estimate" if effective_observations is None else None,
        "window_start_ns": int(times[0]),
        "window_end_ns": int(times[-1]),
        "daily_return_days": sorted(daily),
        "daily_returns": [float(value) for value in daily_returns],
        "coverage_gaps": gaps,
        "metrics": metrics,
        "statistical_significance": "not_evaluated",
    }
