"""Volatility indicators: realized vol, EWMA vol, parkinson/garman-klass, vol-ratio."""

from __future__ import annotations

import numpy as np
import pandas as pd


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1)).rename("logret")


def realized_vol(close: pd.Series, window: int = 24, annualize_factor: float = 365 * 24) -> pd.Series:
    """Rolling realized volatility (std of log returns), annualized."""

    r = log_returns(close)
    rv = r.rolling(window).std(ddof=0) * np.sqrt(annualize_factor)
    return rv.rename("realized_vol")


def ewma_vol(close: pd.Series, alpha: float = 0.06, annualize_factor: float = 365 * 24) -> pd.Series:
    r = log_returns(close)
    var = (r.pow(2)).ewm(alpha=alpha, adjust=False).mean()
    return (np.sqrt(var) * np.sqrt(annualize_factor)).rename("ewma_vol")


def rv_ratio(close: pd.Series, fast: int = 24, slow: int = 24 * 30) -> pd.Series:
    """Ratio of short-window realized vol to long-window realized vol.

    >2 indicates an acute volatility regime spike vs the baseline.
    """

    rv_fast = realized_vol(close, window=fast)
    rv_slow = realized_vol(close, window=slow)
    return (rv_fast / rv_slow.replace(0, np.nan)).rename("rv_ratio")


def parkinson_vol(high: pd.Series, low: pd.Series, window: int = 24,
                  annualize_factor: float = 365 * 24) -> pd.Series:
    """Parkinson high-low volatility estimator (less noisy than close-to-close)."""

    rs = (np.log(high / low) ** 2) / (4 * np.log(2))
    return (np.sqrt(rs.rolling(window).mean()) * np.sqrt(annualize_factor)).rename("parkinson_vol")
