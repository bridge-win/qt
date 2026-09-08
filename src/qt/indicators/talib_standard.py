"""TA-Lib-backed standard indicator definitions for research runtimes.

These functions deliberately call the native TA-Lib package rather than
re-implementing its seed, lookback, or Wilder smoothing rules in pandas. QT's
older pandas indicators remain separately named legacy formulas and must not
be treated as mathematically interchangeable with this module.
"""

from __future__ import annotations

import pandas as pd


def atr(high: pd.Series, low: pd.Series, close: pd.Series, *, period: int = 14) -> pd.Series:
    """Return TA-Lib ATR with its native lookback and Wilder initialization."""

    abstract = _talib_abstract()
    values = abstract.ATR({"high": high, "low": low, "close": close}, timeperiod=period)
    return pd.Series(values, index=close.index, name="atr_talib")


def rsi(close: pd.Series, *, period: int = 14) -> pd.Series:
    """Return TA-Lib RSI with its native lookback and Wilder initialization."""

    abstract = _talib_abstract()
    values = abstract.RSI(close, timeperiod=period)
    return pd.Series(values, index=close.index, name="rsi_talib")


def adx(high: pd.Series, low: pd.Series, close: pd.Series, *, period: int = 14) -> pd.Series:
    """Return TA-Lib ADX with its native lookback and Wilder initialization."""

    abstract = _talib_abstract()
    values = abstract.ADX({"high": high, "low": low, "close": close}, timeperiod=period)
    return pd.Series(values, index=close.index, name="adx_talib")


def _talib_abstract() -> object:
    try:
        import talib.abstract as abstract
    except ImportError as error:  # pragma: no cover - host-dependent optional dependency
        raise RuntimeError(
            "TA-Lib==0.7.1 is required; install qt[native-research] on Python 3.12"
        ) from error
    return abstract
