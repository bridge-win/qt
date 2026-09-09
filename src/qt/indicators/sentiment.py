"""Sentiment indicators built on top of raw sentiment series."""

from __future__ import annotations

import numpy as np
import pandas as pd


def fear_greed_extreme(fng: pd.Series, threshold: int = 15,
                       sustained_days: int = 3) -> pd.Series:
    """True when F&G has been <= threshold for `sustained_days` consecutive days."""

    cond = (fng <= threshold).astype(int)
    return (cond.rolling(sustained_days).sum() >= sustained_days).rename("fng_extreme")


def social_sentiment_z(sentiment: pd.Series, window: int = 30) -> pd.Series:
    mu = sentiment.rolling(window).mean()
    sd = sentiment.rolling(window).std(ddof=0).replace(0, np.nan)
    return ((sentiment - mu) / sd).astype("float64").rename("social_z")


def social_volume_z(volume: pd.Series, window: int = 30) -> pd.Series:
    return social_sentiment_z(volume, window=window).rename("social_volume_z")


def fear_greed_smoothed(fng: pd.Series, days: int = 7) -> pd.Series:
    """7-day mean of F&G — the weekly-cadence version. The raw daily
    ≤15-for-3-days rule has fired on only a handful of days since 2018."""

    return fng.rolling(days, min_periods=max(1, days // 2)).mean().rename("fng_7d")


def fear_greed_greed_extreme(fng: pd.Series, threshold: int = 80,
                             sustained_days: int = 3) -> pd.Series:
    """True when F&G has been ≥ threshold for `sustained_days` days."""

    cond = (fng >= threshold).astype(int)
    return (cond.rolling(sustained_days).sum() >= sustained_days).rename("fng_greed")
