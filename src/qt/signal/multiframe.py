"""Multi-timeframe signal confirmation.

The bot computes the composite extreme score on the *primary* timeframe
(1h by default). Multi-timeframe confirmation requires that 4h and 1d
RSI also be in oversold territory at the same wall-clock bar — empirical
crypto-quant work (Kakushadze & Serur 2023; Cohen 2023) shows 3-TF
confluence dramatically reduces false-positive entries relative to any
single timeframe.

Usage:
    from qt.signal.multiframe import multitf_confirm
    ok = multitf_confirm(close_1h, rsi_max_1h=25, rsi_max_4h=30, rsi_max_1d=35)
"""

from __future__ import annotations

import pandas as pd

from qt.indicators.price import rsi


def _resample_rsi(close_1h: pd.Series, rule: str, period: int = 14) -> pd.Series:
    """Compute RSI on `rule`-resampled close (last), reindex back to 1h."""

    rs = close_1h.resample(rule).last().dropna()
    return rsi(rs, period=period).reindex(close_1h.index, method="ffill")


def multitf_confirm(
    close_1h: pd.Series,
    rsi_max_1h: float = 25.0,
    rsi_max_4h: float = 30.0,
    rsi_max_1d: float = 35.0,
    period: int = 14,
) -> pd.Series:
    """True at bars where 1h, 4h, and daily RSI are all below their thresholds.

    Aligned to the 1h index.
    """

    r1 = rsi(close_1h, period=period)
    r4 = _resample_rsi(close_1h, "4h", period)
    rd = _resample_rsi(close_1h, "1D", period)
    out = (r1 <= rsi_max_1h) & (r4 <= rsi_max_4h) & (rd <= rsi_max_1d)
    return out.fillna(False).rename("mtf_rsi_confirm")
