"""Tests for the four solution-gallery strategies.

Synthetic data fixtures cover each strategy's expected behavior:
- SmartDCA buys more during synthetic fear regimes.
- Capitulation enters in tranches on a fabricated capitulation window.
- WeeklyTrend goes long during an uptrend, exits below SMA.
- BasisCarry collects positive funding and exits on regime flip.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qt.strategies import (
    BasisCarry,
    BasisCarryConfig,
    Capitulation,
    CapitulationConfig,
    SmartDCA,
    SmartDCAConfig,
    WeeklyTrend,
    WeeklyTrendConfig,
)


def _hourly_index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2022-01-03", periods=n, freq="1h", tz="UTC")  # Mon 00:00


def _synth_ohlcv(prices: np.ndarray) -> pd.DataFrame:
    idx = _hourly_index(len(prices))
    high = prices * 1.005
    low = prices * 0.995
    open_ = np.r_[prices[0], prices[:-1]]
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": prices,
         "volume": np.ones(len(prices))},
        index=idx,
    )


# --------------------------- Smart DCA -----------------------------------


def test_smart_dca_runs_and_accumulates() -> None:
    n = 24 * 7 * 12       # 12 weeks of hourly bars
    prices = np.linspace(20_000, 30_000, n)
    ohlcv = _synth_ohlcv(prices)
    cfg = SmartDCAConfig(
        base_buy_quote=50.0, multiplier_k=1.5, initial_cash=2_000.0,
    )
    out = SmartDCA(cfg).run(ohlcv)
    # 12 buys (one per week) -> position grows; equity > cash spent net of fees
    assert (out.trades["side"] == "buy").sum() >= 10
    assert out.equity.iloc[-1] > 0
    assert out.target_weight.iloc[-1] > 0.0


def test_smart_dca_multiplier_higher_in_fear() -> None:
    # Falling price drives drawdown / MA-below-price stress signals up.
    n = 24 * 7 * 16
    prices = np.linspace(60_000, 25_000, n)
    ohlcv = _synth_ohlcv(prices)
    out = SmartDCA(SmartDCAConfig(base_buy_quote=50.0)).run(ohlcv)
    mults = out.diagnostics.loc[out.diagnostics["buy_bar"], "multiplier"]
    assert mults.iloc[-1] > mults.iloc[0]


def test_smart_dca_with_fear_greed_input() -> None:
    n = 24 * 7 * 8
    prices = np.full(n, 30_000.0)
    ohlcv = _synth_ohlcv(prices)
    # Fear & greed dipping into "extreme fear" in the second half.
    fg = pd.Series(
        np.r_[np.full(n // 2, 60.0), np.full(n - n // 2, 10.0)],
        index=ohlcv.index,
    )
    out = SmartDCA(SmartDCAConfig(base_buy_quote=50.0)).run(
        ohlcv, fear_greed=fg,
    )
    diag = out.diagnostics
    buys = diag.loc[diag["buy_bar"]]
    # Last 4 weeks (extreme fear) should average a higher multiplier than first 4
    assert buys.iloc[-4:]["multiplier"].mean() > buys.iloc[:4]["multiplier"].mean()


# --------------------------- Capitulation --------------------------------


def test_capitulation_runs_without_extras() -> None:
    n = 24 * 30
    prices = np.linspace(30_000, 28_000, n)
    ohlcv = _synth_ohlcv(prices)
    out = Capitulation(CapitulationConfig()).run(ohlcv)
    # Without extra inputs, the engine may still fire on price/vol; just
    # assert the result has the right shape.
    assert len(out.equity) == n
    assert out.target_weight.between(0, 1).all()


def test_capitulation_enters_on_strong_trigger() -> None:
    n = 24 * 60
    base = 30_000.0
    # Manufacture a sharp drawdown in the last week:
    prices = np.r_[
        np.full(n - 24 * 7, base),
        np.linspace(base, base * 0.55, 24 * 7),
    ]
    ohlcv = _synth_ohlcv(prices)

    idx = ohlcv.index
    fg = pd.Series(np.r_[np.full(n - 24 * 7, 50), np.full(24 * 7, 8)], index=idx)
    mvrv = pd.Series(np.r_[np.full(n - 24 * 7, 3.0), np.full(24 * 7, -0.5)], index=idx)
    funding = pd.Series(np.r_[np.full(n - 24 * 7, 0.0001),
                              np.full(24 * 7, -0.001)], index=idx)
    nupl = pd.Series(np.r_[np.full(n - 24 * 7, 0.5), np.full(24 * 7, -0.05)], index=idx)

    cfg = CapitulationConfig(
        min_groups_firing=2, score_min=0.3,
        cooldown_bars=1, max_holding_bars=24 * 7,
    )
    out = Capitulation(cfg).run(
        ohlcv, fear_greed=fg, mvrv_z=mvrv, funding=funding, nupl=nupl,
    )
    assert (out.trades["side"] == "buy").sum() >= 1


# --------------------------- Weekly Trend --------------------------------


def test_weekly_trend_long_in_uptrend() -> None:
    n = 24 * 7 * 60      # 60 weeks of hourly bars
    prices = np.linspace(20_000, 60_000, n)
    ohlcv = _synth_ohlcv(prices)
    out = WeeklyTrend(WeeklyTrendConfig(ma_weeks=20)).run(ohlcv)
    # Once the MA fills and price > MA, we should hold a long position.
    assert (out.target_weight.iloc[-24:] > 0).any()


def test_weekly_trend_flat_in_downtrend() -> None:
    n = 24 * 7 * 50
    prices = np.linspace(60_000, 20_000, n)
    ohlcv = _synth_ohlcv(prices)
    out = WeeklyTrend(WeeklyTrendConfig(ma_weeks=10)).run(ohlcv)
    # In a sustained downtrend we expect to be flat for the majority of bars
    assert (out.target_weight == 0).mean() > 0.5


# --------------------------- Basis Carry ---------------------------------


def test_basis_carry_enters_when_funding_high() -> None:
    n = 24 * 30
    prices = np.full(n, 30_000.0)
    ohlcv = _synth_ohlcv(prices)
    # 0.01% per 8h = 0.01 * 3 * 365 = 10.95% APR... below 15% threshold.
    # Use 0.0002 = 0.02% per 8h = 21.9% APR -> above 15% enter.
    funding = pd.Series(0.0002, index=ohlcv.index)
    cfg = BasisCarryConfig(enter_apr=0.15, exit_apr=0.05, avg_window_bars=24)
    out = BasisCarry(cfg).run(ohlcv, funding=funding)
    assert (out.target_weight.iloc[-1] > 0)
    assert (out.short_weight.iloc[-1] > 0)


def test_basis_carry_exits_on_low_funding() -> None:
    n = 24 * 30
    prices = np.full(n, 30_000.0)
    ohlcv = _synth_ohlcv(prices)
    # Start with high funding, then drop to near zero
    fund = pd.Series(0.0, index=ohlcv.index)
    fund.iloc[: n // 2] = 0.0003           # ~33% APR — well above enter
    fund.iloc[n // 2 :] = 0.000005         # ~0.5% APR — well below exit
    cfg = BasisCarryConfig(
        enter_apr=0.15, exit_apr=0.05, avg_window_bars=24, negative_bars=10,
    )
    out = BasisCarry(cfg).run(ohlcv, funding=fund)
    assert out.target_weight.iloc[-1] == 0.0
    assert out.short_weight.iloc[-1] == 0.0


def test_basis_carry_collects_funding_pnl() -> None:
    n = 24 * 30
    prices = np.full(n, 30_000.0)
    ohlcv = _synth_ohlcv(prices)
    # Persistent +30% APR funding -> should grow equity above initial cash
    fund = pd.Series(0.000275, index=ohlcv.index)  # ~30% APR
    cfg = BasisCarryConfig(
        enter_apr=0.10, exit_apr=0.02, avg_window_bars=12,
        fee_bps=0.0, slippage_bps=0.0,
    )
    out = BasisCarry(cfg).run(ohlcv, funding=fund)
    assert out.equity.iloc[-1] > cfg.initial_cash


@pytest.mark.parametrize(
    "strategy_cls,cfg_cls",
    [
        (SmartDCA, SmartDCAConfig),
        (Capitulation, CapitulationConfig),
        (WeeklyTrend, WeeklyTrendConfig),
    ],
)
def test_strategy_outputs_have_consistent_shape(strategy_cls, cfg_cls) -> None:
    n = 24 * 7 * 25
    prices = 30_000 + 1000 * np.sin(np.linspace(0, 6.28, n))
    ohlcv = _synth_ohlcv(prices)
    out = strategy_cls(cfg_cls()).run(ohlcv)
    assert len(out.equity) == n
    assert out.target_weight.index.equals(out.equity.index)
    assert out.short_weight.index.equals(out.equity.index)
