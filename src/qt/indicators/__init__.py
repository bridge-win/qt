"""Indicator computation. Pure functions on pandas DataFrames / Series.

All indicators are computed point-in-time (no look-ahead). Tests in
`tests/test_indicators_*` enforce this with synthetic walk-forward fixtures.
"""

from qt.indicators.composite import ExtremeScore, compute_extreme_score
from qt.indicators.price import (
    atr,
    bollinger_bands,
    bollinger_zscore,
    drawdown_from_high,
    rsi,
    wick_ratio,
)
from qt.indicators.volatility import realized_vol, rv_ratio

__all__ = [
    "ExtremeScore",
    "atr",
    "bollinger_bands",
    "bollinger_zscore",
    "compute_extreme_score",
    "drawdown_from_high",
    "realized_vol",
    "rsi",
    "rv_ratio",
    "wick_ratio",
]
