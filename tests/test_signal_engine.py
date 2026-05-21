"""SignalEngine behaviour tests."""

from __future__ import annotations

import pandas as pd

from qt.core.config import ThresholdConfig
from qt.core.types import SignalKind
from qt.signal.engine import SignalEngine


def test_signal_emits_entry_long_when_score_high(synthetic_ohlcv: pd.DataFrame) -> None:
    # Force trigger by loosening thresholds and providing extra inputs.
    cfg = ThresholdConfig(
        rsi_oversold=40, bb_std=1.0, drawdown_30d_min=0.03,
        wick_body_ratio_min=1.0, rv_ratio_min=1.0,
        entry_score_min=0.1, min_factor_groups=1,
        vix_max=999, dxy_z_max=999,
    )
    # Synthetic Fear & Greed = 5 across the board (extreme fear) -> sentiment fires.
    fg = pd.Series(5, index=synthetic_ohlcv.index)
    eng = SignalEngine(thresholds=cfg)
    score = eng.evaluate(ohlcv=synthetic_ohlcv, fear_greed=fg)
    sigs = eng.to_signals(score)
    assert sigs, "expected at least one ENTRY_LONG signal"
    assert all(s.kind == SignalKind.ENTRY_LONG for s in sigs)


def test_signal_respects_threshold(synthetic_ohlcv: pd.DataFrame) -> None:
    cfg = ThresholdConfig(entry_score_min=0.99, min_factor_groups=5,
                          vix_max=999, dxy_z_max=999)
    eng = SignalEngine(thresholds=cfg)
    score = eng.evaluate(ohlcv=synthetic_ohlcv)
    assert eng.to_signals(score) == []
