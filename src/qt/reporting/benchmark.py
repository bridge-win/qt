"""The honest benchmark: did the strategy beat *just buying*?

Ground rule #3 in the roadmap: the benchmark is plain DCA. If a strategy
can't beat naive weekly fixed-dollar buying after fees over a full cycle,
it should be turned off — no sunk-cost defense.

This module builds that benchmark and produces a side-by-side comparison
so the answer is automatic, not a matter of opinion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from qt.backtest.metrics import compute_metrics


def dca_benchmark_equity(
    ohlcv: pd.DataFrame,
    *,
    initial_cash: float = 10_000.0,
    weekly_buy_quote: float = 100.0,
    buy_dow: int = 0,          # 0 = Monday
    buy_hour_utc: int = 14,
    fee_bps: float = 12.5,
) -> pd.Series:
    """Equity curve of the dumbest possible strategy: buy a fixed $ every week.

    Deploys ``weekly_buy_quote`` of cash into BTC at the first bar on
    ``buy_dow`` at/after ``buy_hour_utc`` each week, until cash runs out.
    Everything not yet deployed sits in cash (0% yield). This is the bar
    every active strategy must clear.
    """
    if ohlcv.empty:
        return pd.Series(dtype="float64")

    close = ohlcv["close"]
    idx = close.index
    cash = float(initial_cash)
    btc = 0.0
    cost = fee_bps / 10_000.0

    equity_pts: list[float] = []
    bought_weeks: set[tuple[int, int]] = set()

    for ts, px in close.items():
        px_f = float(px)
        # Is this a scheduled buy bar we haven't hit this ISO-week yet?
        ts_pd = pd.Timestamp(ts)
        iso = ts_pd.isocalendar()
        week_key = (int(iso.year), int(iso.week))
        is_buy_bar = (
            ts_pd.dayofweek == buy_dow
            and ts_pd.hour >= buy_hour_utc
            and week_key not in bought_weeks
        )
        if is_buy_bar and cash > 0:
            spend = min(weekly_buy_quote, cash)
            fee = spend * cost
            btc += (spend - fee) / px_f
            cash -= spend
            bought_weeks.add(week_key)
        equity_pts.append(cash + btc * px_f)

    return pd.Series(equity_pts, index=idx, name="dca_benchmark")


@dataclass
class BenchmarkComparison:
    """Strategy vs plain-DCA, with a clear verdict."""

    strategy: str
    strategy_final: float
    benchmark_final: float
    strategy_return: float
    benchmark_return: float
    strategy_max_dd: float
    benchmark_max_dd: float
    strategy_sharpe: float
    benchmark_sharpe: float
    beats_benchmark: bool
    verdict: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "strategy_final": self.strategy_final,
            "benchmark_final": self.benchmark_final,
            "strategy_return": self.strategy_return,
            "benchmark_return": self.benchmark_return,
            "strategy_max_dd": self.strategy_max_dd,
            "benchmark_max_dd": self.benchmark_max_dd,
            "strategy_sharpe": self.strategy_sharpe,
            "benchmark_sharpe": self.benchmark_sharpe,
            "beats_benchmark": self.beats_benchmark,
            "verdict": self.verdict,
        }


def compare_to_dca_benchmark(
    strategy_name: str,
    strategy_equity: pd.Series,
    ohlcv: pd.DataFrame,
    *,
    initial_cash: float = 10_000.0,
    weekly_buy_quote: float = 100.0,
) -> BenchmarkComparison:
    """Compare a strategy's equity curve against plain DCA over the same window.

    "Beats benchmark" requires a higher final equity AND a Sharpe at least as
    good — an active strategy that only wins by taking more risk hasn't really
    earned its complexity.
    """
    bench = dca_benchmark_equity(
        ohlcv, initial_cash=initial_cash, weekly_buy_quote=weekly_buy_quote,
    )
    empty_trades = pd.DataFrame(columns=["pnl"])
    s_metrics = compute_metrics(strategy_equity, empty_trades)
    b_metrics = compute_metrics(bench, empty_trades)

    s_final = float(strategy_equity.iloc[-1]) if len(strategy_equity) else 0.0
    b_final = float(bench.iloc[-1]) if len(bench) else 0.0

    beats = (s_final > b_final) and (s_metrics.sharpe >= b_metrics.sharpe - 1e-9)
    if beats:
        verdict = (
            f"{strategy_name} BEAT plain DCA "
            f"({s_final:,.0f} vs {b_final:,.0f}) — keep it."
        )
    elif s_final > b_final:
        verdict = (
            f"{strategy_name} had higher final equity but worse risk-adjusted "
            f"return (Sharpe {s_metrics.sharpe:.2f} vs {b_metrics.sharpe:.2f}) — "
            "the edge came from taking more risk, not skill."
        )
    else:
        verdict = (
            f"{strategy_name} LOST to plain DCA "
            f"({s_final:,.0f} vs {b_final:,.0f}) — turn it off unless it improves."
        )

    return BenchmarkComparison(
        strategy=strategy_name,
        strategy_final=s_final,
        benchmark_final=b_final,
        strategy_return=s_metrics.total_return,
        benchmark_return=b_metrics.total_return,
        strategy_max_dd=s_metrics.max_drawdown,
        benchmark_max_dd=b_metrics.max_drawdown,
        strategy_sharpe=s_metrics.sharpe,
        benchmark_sharpe=b_metrics.sharpe,
        beats_benchmark=beats,
        verdict=verdict,
    )
