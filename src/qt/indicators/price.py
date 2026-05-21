"""Price-action indicators: RSI, Bollinger Bands, ATR, drawdown, wick ratio.

All functions return a Series/DataFrame aligned to the input index.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Uses EWMA-style smoothing (alpha = 1/period)."""

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # When there have been no down-bars, avg_loss == 0 -> rs NaN -> RSI 100.
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    # When there have been no up-bars, avg_gain == 0 -> RSI 0.
    out = out.where(~((avg_gain == 0) & (avg_loss > 0)), 0.0)
    return out.rename("rsi").clip(0, 100)


def bollinger_bands(close: pd.Series, period: int = 20, n_std: float = 2.0) -> pd.DataFrame:
    """Returns ['bb_mid', 'bb_upper', 'bb_lower', 'bb_width']."""

    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + n_std * std
    lower = mid - n_std * std
    width = (upper - lower) / mid
    return pd.DataFrame({"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_width": width})


def bollinger_zscore(close: pd.Series, period: int = 20) -> pd.Series:
    """Standardized distance from rolling mean — negative = below mean."""

    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0).replace(0, np.nan)
    return ((close - mid) / std).rename("bb_z")


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's ATR. Uses true range and EWMA smoothing."""

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().rename("atr")


def drawdown_from_high(close: pd.Series, window: int = 30 * 24) -> pd.Series:
    """Rolling drawdown vs rolling N-bar high. Returns negative fractions (e.g. -0.18)."""

    roll_max = close.rolling(window, min_periods=1).max()
    return ((close - roll_max) / roll_max).rename("drawdown")


def wick_ratio(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Lower-wick to body ratio. >3 = long lower shadow ("hammer" / 插针).

    body = |close - open|; if body == 0 we fall back to a tiny epsilon to
    avoid div-by-zero (giving very large ratios when there's a true wick).
    """

    body = (close - open_).abs()
    lower_wick = pd.concat([open_, close], axis=1).min(axis=1) - low
    eps = (high - low).abs().clip(lower=1e-9) * 1e-3
    safe_body = body.where(body > 0, eps)
    return (lower_wick.clip(lower=0) / safe_body).rename("wick_ratio")
