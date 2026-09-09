"""Derivatives-derived indicators: funding extremes, OI shocks, liquidation regime."""

from __future__ import annotations

import numpy as np
import pandas as pd


def funding_zscore(funding: pd.Series, window: int = 30 * 3) -> pd.Series:
    """Rolling Z-score of 8h funding rate."""

    mu = funding.rolling(window).mean()
    sd = funding.rolling(window).std(ddof=0).replace(0, np.nan)
    return ((funding - mu) / sd).astype("float64").rename("funding_z")


def funding_sustained_negative(funding: pd.Series, bars: int = 3,
                               threshold: float = -0.0001) -> pd.Series:
    """True when funding has been below `threshold` for `bars` consecutive readings."""

    cond = (funding <= threshold).astype(int)
    return (cond.rolling(bars).sum() >= bars).rename("funding_sustained_neg")


def oi_drop_24h(oi: pd.Series, bars_24h: int = 24) -> pd.Series:
    """Fractional drop in open interest over the last 24h."""

    return ((oi - oi.shift(bars_24h)) / oi.shift(bars_24h)).rename("oi_chg_24h")


def long_short_extreme(lsr: pd.Series, window: int = 24 * 30) -> pd.Series:
    """Long/short ratio percentile rank — low = shorts crowded (bullish contrarian)."""

    return lsr.rolling(window).rank(pct=True).rename("lsr_pct")


# --- Additions (2026-09): liquidation regime + overheat side --------------

def liquidation_zscore(liq_usd: pd.Series, window: int = 24 * 30) -> pd.Series:
    """Rolling Z-score of liquidation USD volume (long or short side).

    Liquidation feeds are sampled (Binance pushes ≤1 forceOrder/s since
    2021) so absolute levels are unreliable; the *relative* spike is the
    usable signal. Z ≥ 3 on long liquidations = cascade in progress.
    """

    liq = liq_usd.fillna(0.0)
    mu = liq.rolling(window, min_periods=window // 4).mean()
    sd = liq.rolling(window, min_periods=window // 4).std(ddof=0).replace(0, np.nan)
    return ((liq - mu) / sd).astype("float64").rename("liq_z")


def liquidation_cascade(long_liq_usd: pd.Series, short_liq_usd: pd.Series | None = None,
                        z_min: float = 3.0, dominance_min: float = 0.7,
                        window: int = 24 * 30) -> pd.Series:
    """True when long liquidations spike (Z ≥ z_min) and dominate the
    long+short mix (≥ dominance_min) — i.e. forced long selling, the
    condition under which the short-horizon reversal edge exists."""

    z = liquidation_zscore(long_liq_usd, window=window)
    cond = z.fillna(0) >= z_min
    if short_liq_usd is not None:
        total = long_liq_usd.fillna(0) + short_liq_usd.fillna(0)
        dom = (long_liq_usd.fillna(0) / total.replace(0, np.nan)).fillna(0)
        cond &= dom >= dominance_min
    return cond.rename("liq_cascade")


def oi_surge_24h(oi: pd.Series, bars_24h: int = 24, threshold: float = 0.15) -> pd.Series:
    """Fractional OI *increase* over 24h ≥ threshold — leverage build-up
    that precedes long squeezes (overheat side)."""

    chg = (oi - oi.shift(bars_24h)) / oi.shift(bars_24h)
    return (chg >= threshold).rename("oi_surge_24h")


def funding_sustained_positive(funding: pd.Series, bars: int = 3,
                               threshold: float = 0.0005) -> pd.Series:
    """Funding ≥ +0.05%/8h for `bars` prints = crowded longs paying up."""

    cond = (funding >= threshold).astype(int)
    return (cond.rolling(bars).sum() >= bars).rename("funding_sustained_pos")
