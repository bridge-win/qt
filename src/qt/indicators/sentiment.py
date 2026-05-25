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
