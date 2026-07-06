"""Unified strategy-backtest pipeline tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from qt.backtest.strategy_backtest import (
    BacktestOutcome,
    canonical_strategy,
    run_strategy_backtest,
    synthetic_btc_ohlcv,
    synthetic_funding,
    write_strategy_backtest_artifacts,
)


def test_canonical_strategy_aliases() -> None:
    assert canonical_strategy("dca") == "dca"
    assert canonical_strategy("A") == "dca"
    assert canonical_strategy("smart_dca") == "dca"
    assert canonical_strategy("weekly") == "trend"
    assert canonical_strategy("basis") == "carry"


def test_canonical_strategy_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        canonical_strategy("nonsense")
    with pytest.raises(ValueError, match="capitulation"):
        canonical_strategy("capitulation")


def test_synthetic_data_is_sane() -> None:
    df = synthetic_btc_ohlcv(days=365)
    assert len(df) == 365 * 24
    assert (df["close"] > 0).all()
    # Price should not collapse toward zero or explode.
    assert df["close"].min() > 100
    assert df["close"].max() < 10_000_000
    # High >= max(open, close), low <= min(open, close)
    assert (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-6).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1) + 1e-6).all()


def test_synthetic_data_is_deterministic() -> None:
    a = synthetic_btc_ohlcv(days=30, seed=3)
    b = synthetic_btc_ohlcv(days=30, seed=3)
    assert a["close"].equals(b["close"])


@pytest.mark.parametrize("strat", ["dca", "trend", "carry"])
def test_run_strategy_backtest_synthetic(strat: str) -> None:
    out = run_strategy_backtest(strat, None, allow_synthetic=True, synthetic_days=365)
    assert isinstance(out, BacktestOutcome)
    assert out.synthetic is True
    assert out.strategy == strat
    assert len(out.equity) > 0
    assert out.equity.iloc[0] > 0
    # Summary is JSON-serializable-ish
    summ = out.summary()
    assert summ["strategy"] == strat
    assert "metrics" in summ


def test_run_strategy_backtest_no_synthetic_raises() -> None:
    with pytest.raises(ValueError, match="no OHLCV"):
        run_strategy_backtest("dca", None, allow_synthetic=False)


def test_run_carry_with_supplied_funding() -> None:
    ohlcv = synthetic_btc_ohlcv(days=120)
    funding = synthetic_funding(ohlcv.index)
    out = run_strategy_backtest("carry", ohlcv, funding=funding, allow_synthetic=False)
    assert out.synthetic is False
    assert len(out.equity) > 0


def test_write_artifacts(tmp_path: Path) -> None:
    out = run_strategy_backtest("dca", None, allow_synthetic=True, synthetic_days=90)
    run_dir = write_strategy_backtest_artifacts(out, tmp_path)
    assert (run_dir / "equity.csv").exists()
    assert (run_dir / "trades.csv").exists()
    assert (run_dir / "summary.json").exists()
    assert (tmp_path / "strategy_latest.json").exists()
