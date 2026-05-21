"""Standard backtest performance metrics."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Metrics:
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    num_trades: int
    avg_holding_bars: float


def equity_to_returns(equity: pd.Series) -> pd.Series:
    return equity.pct_change().dropna()


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(dd.min() if len(dd) else 0.0)


def sharpe_ratio(returns: pd.Series, periods_per_year: int = 365 * 24) -> float:
    if returns.std(ddof=0) == 0 or returns.empty:
        return 0.0
    return float(returns.mean() / returns.std(ddof=0) * np.sqrt(periods_per_year))


def sortino_ratio(returns: pd.Series, periods_per_year: int = 365 * 24) -> float:
    if returns.empty:
        return 0.0
    downside = returns.clip(upper=0)
    denom = downside.std(ddof=0)
    if denom == 0:
        return 0.0
    return float(returns.mean() / denom * np.sqrt(periods_per_year))


def compute_metrics(equity: pd.Series, trades: pd.DataFrame,
                    periods_per_year: int = 365 * 24) -> Metrics:
    if equity.empty:
        return Metrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    rets = equity_to_returns(equity)
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    years = (equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 24 * 3600)
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else 0.0
    mdd = max_drawdown(equity)
    sh = sharpe_ratio(rets, periods_per_year)
    so = sortino_ratio(rets, periods_per_year)
    cal = (cagr / abs(mdd)) if mdd < 0 else 0.0

    if trades.empty:
        return Metrics(total, cagr, sh, so, cal, mdd, 0, 0, 0, 0, 0, 0)

    pnl = trades["pnl"]
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    win_rate = float(len(wins) / len(pnl)) if len(pnl) else 0.0
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) else float("inf")
    hold = float(trades["holding_bars"].mean()) if "holding_bars" in trades else 0.0

    return Metrics(total, cagr, sh, so, cal, mdd, win_rate, avg_win, avg_loss, pf, len(pnl), hold)
