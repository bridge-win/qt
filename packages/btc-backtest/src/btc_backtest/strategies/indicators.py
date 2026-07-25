"""Pure point-in-time technical indicators used by built-in strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(values: pd.Series, window: int) -> pd.Series:
    """Simple moving average with no pre-warm-up values."""

    _window(window)
    return values.astype("float64").rolling(
        window=window,
        min_periods=window,
    ).mean()


def ema(values: pd.Series, window: int) -> pd.Series:
    """Exponentially weighted average using the standard span convention."""

    _window(window)
    return values.astype("float64").ewm(
        span=window,
        adjust=False,
        min_periods=window,
    ).mean()


def rsi(close: pd.Series, window: int) -> pd.Series:
    """Wilder relative-strength index bounded to ``[0, 100]``."""

    _window(window)
    delta = close.astype("float64").diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    average_gain = _wilder(gain, window)
    average_loss = _wilder(loss, window)
    relative_strength = average_gain / average_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + relative_strength))
    result = result.mask(
        (average_loss == 0) & (average_gain > 0),
        100.0,
    )
    result = result.mask(
        (average_loss == 0) & (average_gain == 0),
        50.0,
    )
    return result.clip(lower=0, upper=100)


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_window: int,
    d_window: int,
) -> tuple[pd.Series, pd.Series]:
    """Fast stochastic ``%K`` and its simple ``%D`` signal."""

    _aligned(high, low, close)
    _window(k_window)
    _window(d_window)
    lowest = low.astype("float64").rolling(
        k_window,
        min_periods=k_window,
    ).min()
    highest = high.astype("float64").rolling(
        k_window,
        min_periods=k_window,
    ).max()
    spread = (highest - lowest).replace(0, np.nan)
    percent_k = (100 * (close.astype("float64") - lowest) / spread).clip(
        lower=0,
        upper=100,
    )
    percent_d = sma(percent_k, d_window)
    return percent_k, percent_d


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, and histogram."""

    _window(fast)
    _window(slow)
    _window(signal)
    if fast >= slow:
        raise ValueError("MACD fast window must be below slow window")
    line = ema(close, fast) - ema(close, slow)
    signal_line = ema(line, signal)
    return line, signal_line, line - signal_line


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int,
) -> pd.Series:
    """Wilder average true range."""

    _aligned(high, low, close)
    _window(window)
    high_values = high.astype("float64")
    low_values = low.astype("float64")
    previous_close = close.astype("float64").shift(1)
    true_range = pd.concat(
        (
            high_values - low_values,
            (high_values - previous_close).abs(),
            (low_values - previous_close).abs(),
        ),
        axis=1,
    ).max(axis=1)
    return _wilder(true_range, window)


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder ADX, positive directional index, and negative index."""

    _aligned(high, low, close)
    _window(window)
    high_values = high.astype("float64")
    low_values = low.astype("float64")
    upward = high_values.diff()
    downward = -low_values.diff()
    plus_movement = upward.where(
        (upward > downward) & (upward > 0),
        0.0,
    )
    minus_movement = downward.where(
        (downward > upward) & (downward > 0),
        0.0,
    )
    average_range = atr(high_values, low_values, close, window)
    plus_di = 100 * _wilder(plus_movement, window) / average_range
    minus_di = 100 * _wilder(minus_movement, window) / average_range
    denominator = (plus_di + minus_di).replace(0, np.nan)
    directional_strength = 100 * (plus_di - minus_di).abs() / denominator
    return _wilder(directional_strength, window), plus_di, minus_di


def bollinger(
    close: pd.Series,
    window: int,
    standard_deviations: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Population-standard-deviation Bollinger bands."""

    _window(window)
    _positive_number(standard_deviations, "standard_deviations")
    middle = sma(close, window)
    deviation = close.astype("float64").rolling(
        window,
        min_periods=window,
    ).std(ddof=0)
    width = deviation * standard_deviations
    return middle - width, middle, middle + width


def donchian(
    high: pd.Series,
    low: pd.Series,
    window: int,
) -> tuple[pd.Series, pd.Series]:
    """Rolling Donchian lower and upper channel."""

    _aligned(high, low)
    _window(window)
    lower = low.astype("float64").rolling(
        window,
        min_periods=window,
    ).min()
    upper = high.astype("float64").rolling(
        window,
        min_periods=window,
    ).max()
    return lower, upper


def keltner(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    ema_window: int,
    atr_window: int,
    multiplier: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """EMA-centered Keltner channel."""

    _aligned(high, low, close)
    _window(ema_window)
    _window(atr_window)
    _positive_number(multiplier, "multiplier")
    middle = ema(close, ema_window)
    width = atr(high, low, close, atr_window) * multiplier
    return middle - width, middle, middle + width


def rolling_vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    window: int,
) -> pd.Series:
    """Rolling typical-price VWAP, with zero-volume windows left undefined."""

    _aligned(high, low, close, volume)
    _window(window)
    volume_values = volume.astype("float64")
    typical = (
        high.astype("float64")
        + low.astype("float64")
        + close.astype("float64")
    ) / 3
    numerator = (typical * volume_values).rolling(
        window,
        min_periods=window,
    ).sum()
    denominator = volume_values.rolling(
        window,
        min_periods=window,
    ).sum()
    return numerator / denominator.replace(0, np.nan)


def roc(close: pd.Series, lookback: int) -> pd.Series:
    """Fractional rate of change over ``lookback`` completed bars."""

    _window(lookback)
    return close.astype("float64").pct_change(
        periods=lookback,
        fill_method=None,
    )


def _wilder(values: pd.Series, window: int) -> pd.Series:
    return values.ewm(
        alpha=1 / window,
        adjust=False,
        min_periods=window,
    ).mean()


def _window(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("indicator window must be a positive integer")


def _aligned(*values: pd.Series) -> None:
    if not values:
        return
    first = values[0].index
    if any(not item.index.equals(first) for item in values[1:]):
        raise ValueError("indicator inputs must have identical indexes")


def _positive_number(value: float, field: str) -> None:
    if not np.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be finite and positive")
