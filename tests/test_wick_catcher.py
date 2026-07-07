"""Wick Catcher tests — live signal generator + sim backtest."""

from __future__ import annotations

import pandas as pd

from qt.backtest.strategy_backtest import run_strategy_backtest
from qt.strategies.base import StrategyConfig
from qt.strategies.sim.wick_catcher import WickCatcher as WickBacktest
from qt.strategies.sim.wick_catcher import WickCatcherConfig
from qt.strategies.wick_catcher import WickCatcher


def _ohlcv(closes: list[float], lows: list[float] | None = None) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="1h", tz="UTC")
    close = pd.Series(closes, index=idx, dtype=float)
    low = pd.Series(lows, index=idx, dtype=float) if lows else close * 0.999
    open_ = close.shift(1).fillna(close.iloc[0])
    return pd.DataFrame({
        "open": open_,
        "high": pd.concat([open_, close], axis=1).max(axis=1) * 1.001,
        "low": low,
        "close": close,
        "volume": 100.0,
    }, index=idx)


def _strategy(**params) -> WickCatcher:
    return WickCatcher(StrategyConfig(name="wick", params=params))


# ---- live signal generator ---------------------------------------------------

def test_watch_when_no_rung_touched() -> None:
    s = _strategy()
    data = {"ohlcv": _ohlcv([50_000.0] * 30)}
    result = s.evaluate(data)
    assert result.opportunity is None
    assert result.metrics["in_position"] is False
    assert len(result.metrics["rungs"]) == 3


def test_buy_emitted_when_wick_pierces_rung() -> None:
    s = _strategy(rungs_pct=[0.05, 0.08, 0.12])
    closes = [50_000.0] * 30
    lows = [49_950.0] * 29 + [46_000.0]  # last bar wicks -8% below ref
    result = s.evaluate({"ohlcv": _ohlcv(closes, lows)})
    assert result.opportunity is not None
    assert result.opportunity.action == "buy"
    assert len(result.opportunity.details["rungs_hit"]) == 2  # -5% and -8%
    assert "forced sellers" in result.opportunity.reason


def test_take_profit_sell_after_reversion() -> None:
    s = _strategy(rungs_pct=[0.05], take_profit_pct=0.05)
    # First cycle: wick to -6% -> buy at rung 47500
    closes = [50_000.0] * 30
    lows = [49_950.0] * 29 + [47_000.0]
    r1 = s.evaluate({"ohlcv": _ohlcv(closes, lows)})
    assert r1.opportunity is not None and r1.opportunity.action == "buy"
    # Second cycle: price reverts above take-profit (47500*1.05 = 49875)
    r2 = s.evaluate({"ohlcv": _ohlcv([50_000.0] * 30)})
    assert r2.opportunity is not None
    assert r2.opportunity.action == "sell"
    assert "take-profit" in r2.opportunity.reason


def test_hard_stop_on_regime_break() -> None:
    s = _strategy(rungs_pct=[0.05], hard_stop_pct=0.05)
    closes = [50_000.0] * 30
    lows = [49_950.0] * 29 + [47_000.0]
    s.evaluate({"ohlcv": _ohlcv(closes, lows)})  # buy
    # Now the market keeps falling: close far below the ladder bottom
    crash = [40_000.0] * 30
    r = s.evaluate({"ohlcv": _ohlcv(crash)})
    assert r.opportunity is not None
    assert r.opportunity.action == "sell"
    assert "regime break" in r.opportunity.reason


def test_no_data_is_soft_watch() -> None:
    s = _strategy()
    r = s.evaluate({"ohlcv": None})
    assert r.opportunity is None


# ---- sim backtest --------------------------------------------------------------

def test_backtest_fills_and_exits_on_synthetic() -> None:
    out = run_strategy_backtest("wick", None, allow_synthetic=True, synthetic_days=365)
    assert out.strategy == "wick"
    assert out.synthetic is True
    buys = out.trades[out.trades["side"] == "buy"]
    sells = out.trades[out.trades["side"] == "sell"]
    assert len(buys) > 0, "synthetic crashes must produce rung fills"
    assert len(sells) > 0
    # Every position that opened eventually closed (flat at the end or one open)
    assert abs(len(buys.groupby(buys["ts"]).size()) - len(sells)) <= 1


def test_backtest_hard_stop_limits_loss() -> None:
    # Straight-down market: ladder fills then hard-stops; loss must stay small
    n = 200
    closes = [50_000.0 * (1 - 0.002 * i) for i in range(n)]
    ohlcv = _ohlcv(closes)
    cfg = WickCatcherConfig(initial_cash=10_000.0, per_rung_alloc=0.03)
    res = WickBacktest(cfg).run(ohlcv)
    total_dd = res.equity.iloc[-1] / res.equity.iloc[0] - 1
    # 3 rungs x 3% each, stopped ~10% below fills -> worst case low-single-digit %
    assert total_dd > -0.10


def test_backtest_alias_resolution() -> None:
    from qt.backtest.strategy_backtest import canonical_strategy

    assert canonical_strategy("wick") == "wick"
    assert canonical_strategy("E") == "wick"
    assert canonical_strategy("wick_catcher") == "wick"
