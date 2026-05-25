"""Bootstrap / Monte Carlo confidence intervals on trade sequences.

Two complementary procedures:

1. **Trade-list bootstrap**: resample the realized trade returns with
   replacement; compute distribution of total-return / max-DD / Sharpe.
   Robust to fat tails when trade count is small.

2. **Synthetic randomization**: shuffle trade timestamps over the test
   window to test the null hypothesis that the strategy adds nothing
   beyond random timing (white-reality / SPA-style).

References:
- Politis & Romano (1994), "The stationary bootstrap".
- White (2000), "A Reality Check for Data Snooping".
- Bailey, Borwein, López de Prado & Zhu (2014), "Probability of Backtest
  Overfitting" — deflated Sharpe ratio.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qt.backtest.metrics import max_drawdown


@dataclass
class BootstrapStats:
    mean_total_return: float
    p05_total_return: float
    p95_total_return: float
    mean_max_drawdown: float
    p05_max_drawdown: float
    p95_max_drawdown: float
    prob_positive_total_return: float
    n_iter: int


def bootstrap_trade_returns(
    trade_returns: np.ndarray,
    n_iter: int = 5_000,
    seed: int = 20251015,
) -> BootstrapStats:
    """Bootstrap statistics over equally-weighted resampled trade returns.

    Treats each trade's net return as one i.i.d. draw and constructs many
    synthetic equity curves of identical length.
    """

    n = len(trade_returns)
    if n == 0:
        return BootstrapStats(0, 0, 0, 0, 0, 0, 0, n_iter)
    rng = np.random.default_rng(seed)
    tot_rets = np.empty(n_iter, dtype=np.float64)
    mdds = np.empty(n_iter, dtype=np.float64)
    for i in range(n_iter):
        sample = rng.choice(trade_returns, size=n, replace=True)
        # Compound returns; assume each "trade" applies sequentially to capital.
        equity = np.cumprod(1.0 + sample)
        tot_rets[i] = equity[-1] - 1.0
        eq_ser = pd.Series(equity)
        mdds[i] = max_drawdown(eq_ser)
    return BootstrapStats(
        mean_total_return=float(tot_rets.mean()),
        p05_total_return=float(np.quantile(tot_rets, 0.05)),
        p95_total_return=float(np.quantile(tot_rets, 0.95)),
        mean_max_drawdown=float(mdds.mean()),
        p05_max_drawdown=float(np.quantile(mdds, 0.05)),
        p95_max_drawdown=float(np.quantile(mdds, 0.95)),
        prob_positive_total_return=float((tot_rets > 0).mean()),
        n_iter=n_iter,
    )


def stationary_bootstrap(
    returns: np.ndarray, n_iter: int = 2_000, block_mean: float = 24.0,
    seed: int = 20251015,
) -> np.ndarray:
    """Politis-Romano stationary bootstrap for bar-level returns.

    Returns an array of shape (n_iter,) of resampled total-return outcomes.
    `block_mean` is the geometric-block mean length (bars).
    """

    rng = np.random.default_rng(seed)
    n = len(returns)
    p_break = 1.0 / max(block_mean, 1.0)
    totals = np.empty(n_iter, dtype=np.float64)
    for i in range(n_iter):
        out = np.empty(n)
        idx = int(rng.integers(0, n))
        for t in range(n):
            out[t] = returns[idx]
            # With prob p_break, restart from a new random index; else step.
            if rng.random() < p_break:
                idx = int(rng.integers(0, n))
            else:
                idx = (idx + 1) % n
        totals[i] = np.prod(1.0 + out) - 1.0
    return totals


def deflated_sharpe(sharpe: float, n_trials: int, sample_size: int,
                    skew: float = 0.0, kurt: float = 3.0) -> float:
    """Deflated Sharpe ratio (Bailey & López de Prado 2014).

    Adjusts an observed Sharpe by the number of trials taken to find it
    (typical of walk-forward grid search) and by the higher moments of
    the return distribution. Returns a probability in [0, 1].
    """

    from scipy.stats import norm

    if n_trials <= 1 or sample_size <= 1:
        return float("nan")
    emc = 0.5772156649
    max_z = (1 - emc) * norm.ppf(1 - 1.0 / n_trials) + \
        emc * norm.ppf(1 - 1.0 / (n_trials * np.e))
    sr_std = np.sqrt(
        (1 - skew * sharpe + ((kurt - 1) / 4.0) * sharpe**2) / (sample_size - 1)
    )
    dsr = norm.cdf((sharpe - max_z * sr_std) / max(sr_std, 1e-12))
    return float(dsr)
