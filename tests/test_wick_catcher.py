"""Wick-catcher live signal and batch simulator tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd

from qt.strategies.base import StrategyConfig
from qt.strategies.sim import WickCatcherBacktest, WickCatcherConfig
from qt.strategies.wick_catcher import WickCatcher


def _wick_ohlcv() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=30, freq="1h", tz="UTC")
    close = pd.Series(np.full(len(idx), 100.0), index=idx)
    ohlcv = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.995,
            "close": close,
            "volume": np.ones(len(idx)),
        }
    )
    ohlcv.iloc[-1, ohlcv.columns.get_loc("open")] = 100.0
    ohlcv.iloc[-1, ohlcv.columns.get_loc("low")] = 91.5
    ohlcv.iloc[-1, ohlcv.columns.get_loc("close")] = 97.5
    return ohlcv


def test_wick_catcher_emits_buy_when_ladder_rung_is_pierced() -> None:
    strategy = WickCatcher(
        StrategyConfig(
            name="wick",
            params={
                "ladder_drawdowns": [0.05, 0.08, 0.12],
                "take_profit_pct": 0.03,
                "rung_quote": 100.0,
            },
        )
    )

    fake_now = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
    with patch("qt.strategies.wick_catcher.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        out = strategy.evaluate({"ohlcv": _wick_ohlcv()})

    assert out.opportunity is not None
    assert out.opportunity.action == "buy"
    assert out.opportunity.details["filled_rungs"] == [0.05, 0.08]
    assert out.metrics["nearest_rung_price"] == 95.0


def test_wick_catcher_backtest_records_fills_and_take_profits(
    synthetic_ohlcv: pd.DataFrame,
) -> None:
    cfg = WickCatcherConfig(
        ladder_drawdowns=(0.05, 0.08, 0.12),
        take_profit_pct=0.03,
        rung_risk_pct=0.03,
        max_open_rungs=3,
    )
    result = WickCatcherBacktest(cfg).run(synthetic_ohlcv)

    assert not result.equity.empty
    assert not result.trades.empty
    assert (result.trades["side"] == "buy").any()
    assert (result.trades["note"].astype(str).str.contains("take_profit")).any()
    assert (result.equity > 0).all()
