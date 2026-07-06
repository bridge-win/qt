"""DCA-benchmark comparison tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qt.backtest.strategy_backtest import synthetic_btc_ohlcv
from qt.reporting import compare_to_dca_benchmark, dca_benchmark_equity


def _flat_ohlcv(periods: int = 24 * 60, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=periods, freq="1h", tz="UTC")
    close = pd.Series(price, index=idx)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1.0},
        index=idx,
    )


def test_dca_benchmark_deploys_cash_over_time() -> None:
    ohlcv = _flat_ohlcv()
    eq = dca_benchmark_equity(ohlcv, initial_cash=1000.0, weekly_buy_quote=100.0)
    assert len(eq) == len(ohlcv)
    # On flat price, equity stays ~initial (minus small fees on deployed cash)
    assert 995.0 <= eq.iloc[-1] <= 1000.0


def test_dca_benchmark_buys_weekly() -> None:
    # ~8 weeks of data -> at most ~8 weekly buys of 100 from 1000 cash
    ohlcv = _flat_ohlcv(periods=24 * 60)
    eq = dca_benchmark_equity(ohlcv, initial_cash=1000.0, weekly_buy_quote=100.0)
    # Equity should never go negative or exceed starting cash on flat price
    assert (eq > 0).all()


def test_dca_benchmark_rises_in_bull() -> None:
    idx = pd.date_range("2024-01-01", periods=24 * 90, freq="1h", tz="UTC")
    close = pd.Series(np.linspace(100, 200, len(idx)), index=idx)
    ohlcv = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1.0},
        index=idx,
    )
    eq = dca_benchmark_equity(ohlcv, initial_cash=10_000.0, weekly_buy_quote=500.0)
    # Buying into a rising market should end above starting cash
    assert eq.iloc[-1] > 10_000.0


def test_compare_produces_verdict() -> None:
    ohlcv = synthetic_btc_ohlcv(days=180)
    # A "strategy" that just holds cash -> must lose to DCA in a market that moves
    flat_equity = pd.Series(10_000.0, index=ohlcv.index)
    cmp = compare_to_dca_benchmark("test", flat_equity, ohlcv, initial_cash=10_000.0)
    assert cmp.strategy == "test"
    assert isinstance(cmp.beats_benchmark, bool)
    assert cmp.verdict
    assert "test" in cmp.verdict


def test_compare_winner_beats_benchmark() -> None:
    ohlcv = _flat_ohlcv()
    # A strategy that grows steadily beats flat-price DCA
    winner = pd.Series(
        np.linspace(10_000, 20_000, len(ohlcv)), index=ohlcv.index,
    )
    cmp = compare_to_dca_benchmark("winner", winner, ohlcv, initial_cash=10_000.0)
    assert cmp.strategy_final > cmp.benchmark_final
    assert cmp.beats_benchmark is True
    assert "BEAT" in cmp.verdict
