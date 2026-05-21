"""Derivatives-derived indicators: funding extremes, OI shocks, liquidation regime."""

from __future__ import annotations

import pandas as pd


def funding_zscore(funding: pd.Series, window: int = 30 * 3) -> pd.Series:
    """Rolling Z-score of 8h funding rate."""

    mu = funding.rolling(window).mean()
    sd = funding.rolling(window).std(ddof=0).replace(0, pd.NA)
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
