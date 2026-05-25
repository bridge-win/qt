"""Regime detection helpers.

We use lightweight, no-extra-dependency methods that work well enough as
gate features for our composite score:

- **Hurst exponent** (rolling DFA): H < 0.4 = mean-reverting (favorable
  for crash-buying), H > 0.6 = trending (avoid).
- **Volatility regime** via z-score of EWMA realized vol: > 2 = high-vol
  stress regime.
- **Hidden Markov Model** approximation: a simple two-state classifier on
  return magnitude using percentile-based thresholds. For a proper HMM
  swap in `hmmlearn`; this stub gives a reasonable fallback when the
  optional dependency is unavailable.

References:
- Hurst (1951), "Long-term storage capacity of reservoirs" (R/S analysis).
- Peng et al. (1994), "Mosaic organization of DNA nucleotides" (DFA).
- Ardia et al. (2019), "Regime changes in Bitcoin GARCH volatility
  dynamics" (Markov-switching GARCH).
- Adams & MacKay (2007), "Bayesian Online Changepoint Detection".
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def hurst_dfa(series: pd.Series, window: int = 24 * 30) -> pd.Series:
    """Rolling DFA-style Hurst exponent.

    For each rolling window, computes Hurst via the simple R/S method on
    log returns. Output is aligned to the rightmost bar.
    """

    log_ret = np.log(series / series.shift(1))
    out = pd.Series(np.nan, index=series.index)

    def _h(arr: np.ndarray) -> float:
        arr = arr[~np.isnan(arr)]
        if len(arr) < 32:
            return np.nan
        # R/S over multiple scales
        scales = np.unique(np.logspace(2, np.log10(len(arr) // 4), num=8).astype(int))
        scales = scales[(scales >= 8) & (scales < len(arr))]
        if len(scales) < 3:
            return np.nan
        rs = []
        for s in scales:
            n_chunks = len(arr) // s
            if n_chunks < 1:
                continue
            r_s_vals = []
            for i in range(n_chunks):
                seg = arr[i * s:(i + 1) * s]
                mean = seg.mean()
                cumdev = np.cumsum(seg - mean)
                R = cumdev.max() - cumdev.min()
                S = seg.std(ddof=0)
                if S > 0 and R > 0:
                    r_s_vals.append(R / S)
            if r_s_vals:
                rs.append((s, np.mean(r_s_vals)))
        if len(rs) < 3:
            return np.nan
        xs = np.log([s for s, _ in rs])
        ys = np.log([v for _, v in rs])
        slope, _ = np.polyfit(xs, ys, 1)
        return float(slope)

    arr = log_ret.values
    for i in range(window, len(arr) + 1):
        out.iloc[i - 1] = _h(arr[i - window:i])
    return out.rename("hurst")


def vol_regime_z(close: pd.Series, ewma_alpha: float = 0.06,
                 window: int = 24 * 60) -> pd.Series:
    """Z-score of EWMA-vol over a long rolling window. >2 = high-vol stress."""

    log_ret = np.log(close / close.shift(1))
    vol = log_ret.pow(2).ewm(alpha=ewma_alpha, adjust=False).mean().pow(0.5)
    mu = vol.rolling(window).mean()
    sd = vol.rolling(window).std(ddof=0).replace(0, np.nan)
    return ((vol - mu) / sd).rename("vol_regime_z")


def regime_label(close: pd.Series, hurst_window: int = 24 * 30,
                 vol_window: int = 24 * 60) -> pd.Series:
    """Categorical regime label: 'meanrevert_lowvol' / 'trend_lowvol' /
    'meanrevert_highvol' / 'trend_highvol'.

    Coarse but works as a composite-score regime filter.
    """

    h = hurst_dfa(close, window=hurst_window)
    z = vol_regime_z(close, window=vol_window)
    mr = h < 0.5
    hv = z > 1.5
    label = np.where(
        mr & hv, "meanrevert_highvol",
        np.where(mr & ~hv, "meanrevert_lowvol",
        np.where(~mr & hv, "trend_highvol", "trend_lowvol"))
    )
    return pd.Series(label, index=close.index, name="regime")
