"""Extreme-event detectors: liquidation cascades, flash crashes, wick clusters.

These are specialized event detectors (yes/no triggers) for use in the
composite-score derivative group or as standalone signal augmentations.

References:
- Coinglass historical liquidation heatmaps (May 19 2021: ~$8.6B 24h longs;
  Nov 9 2022 FTX: ~$1.6B; Jun 12 2022 Celsius: ~$1B). Within 24-72h of each
  cascade peak, BTC retraced 6-18% upward.
- Donier & Bouchaud (2015) "Why Do Markets Crash? Bitcoin Data Offers
  Unprecedented Insights" — liquidity-driven flash crash model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def liquidation_cascade(
    long_liquidations_usd: pd.Series,
    bars_24h: int = 24,
    notional_threshold: float = 400_000_000.0,
    z_threshold: float = 3.0,
    z_window: int = 24 * 30,
) -> pd.Series:
    """Detect a long-liquidation cascade.

    Fires when either:
    - 24h-rolling long liquidations exceed `notional_threshold` (USD), OR
    - 24h-rolling long liquidations Z-score exceeds `z_threshold`.

    Default $400M reflects post-2022 OI levels; raise to $800M-$1B for
    pre-FTX baselines.
    """

    roll = long_liquidations_usd.rolling(bars_24h).sum()
    mu = roll.rolling(z_window).mean()
    sd = roll.rolling(z_window).std(ddof=0).replace(0, np.nan)
    z = (roll - mu) / sd
    return ((roll >= notional_threshold) | (z >= z_threshold)).rename(
        "liq_cascade"
    )


def flash_crash(close: pd.Series, threshold_pct: float = 0.08, bars: int = 4) -> pd.Series:
    """Detect a rolling N-bar return <= -threshold (e.g., -8% in 4h)."""

    ret = close.pct_change(periods=bars)
    return (ret <= -threshold_pct).rename("flash_crash")


def wick_cluster(
    wick_ratio_series: pd.Series, window: int = 6, count: int = 2,
    ratio_min: float = 3.0,
) -> pd.Series:
    """At least `count` long-lower-wick bars in last `window` bars."""

    extreme = (wick_ratio_series >= ratio_min).astype(int)
    return (extreme.rolling(window, min_periods=count).sum() >= count).fillna(False).astype(bool).rename("wick_cluster")


def oi_unwind(
    oi: pd.Series, bars_24h: int = 24, drop_pct: float = 0.10
) -> pd.Series:
    """24h fractional OI decline >= drop_pct."""

    return ((oi - oi.shift(bars_24h)) / oi.shift(bars_24h) <= -drop_pct).rename(
        "oi_unwind"
    )


def basis_backwardation(
    perp_or_quarterly: pd.Series, spot: pd.Series, threshold: float = 0.0
) -> pd.Series:
    """Annualised basis below `threshold` (default 0 = backwardation).

    Rare for BTC and historically marks capitulation (Mar 2020, Nov 2022).
    Inputs must already be aligned and in price terms.
    """

    basis = (perp_or_quarterly - spot) / spot
    return (basis <= threshold).rename("basis_backwardation")


def funding_flush(
    funding: pd.Series, window: int = 9, threshold: float = -0.0001
) -> pd.Series:
    """Sustained extreme negative funding over `window` consecutive prints
    (typically 9 = 3 days at 8h funding intervals)."""

    cond = (funding <= threshold).astype(int)
    return (cond.rolling(window).sum() >= window).rename("funding_flush")


def regime_panic_score(
    flash: pd.Series,
    liq: pd.Series,
    oi_un: pd.Series,
    wick: pd.Series,
    bb_window: int = 4,
) -> pd.Series:
    """Composite "panic" event: any of the cascade detectors firing in last
    `bb_window` bars."""

    cols = []
    for s in (flash, liq, oi_un, wick):
        if s is not None:
            cols.append(s.astype(bool).rolling(bb_window, min_periods=1).max().astype(bool))
    if not cols:
        return pd.Series(dtype="bool")
    out = cols[0]
    for c in cols[1:]:
        out = out | c
    return out.rename("panic_regime")
