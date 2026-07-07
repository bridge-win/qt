"""Intelligence Discovery tests — pure evaluate() paths, no network."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from qt.intel.ranker import rank_findings, read_findings, write_findings
from qt.intel.scanners import (
    BasisScanner,
    DepegScanner,
    FundingScanner,
    SpreadScanner,
    WickScanner,
)
from qt.intel.strategy import IntelDiscovery
from qt.strategies.base import StrategyConfig

# ---- FundingScanner --------------------------------------------------------

def test_funding_carry_detected_when_fat() -> None:
    s = FundingScanner(min_carry_apr=0.10)
    # 0.02%/8h = 21.9% APR
    findings = s.evaluate({"binance": 0.0002})
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "funding_carry"
    assert f.net_edge_bps > 0
    assert "binance" in f.venues
    assert "APR" in f.reason


def test_funding_carry_ignored_when_thin() -> None:
    s = FundingScanner(min_carry_apr=0.10)
    # 0.005%/8h = 5.5% APR < 10% threshold
    assert s.evaluate({"binance": 0.00005}) == []


def test_funding_differential_across_venues() -> None:
    s = FundingScanner(min_diff_apr=0.05)
    findings = s.evaluate({"binance": 0.0003, "okx": -0.0001})
    kinds = {f.kind for f in findings}
    assert "funding_diff" in kinds
    diff = next(f for f in findings if f.kind == "funding_diff")
    assert diff.venues == ("binance", "okx")  # short the high, long the low


def test_negative_funding_no_carry() -> None:
    s = FundingScanner()
    findings = s.evaluate({"binance": -0.0003})
    assert all(f.kind != "funding_carry" for f in findings)


# ---- SpreadScanner ---------------------------------------------------------

def test_spread_detected_net_of_fees() -> None:
    s = SpreadScanner(taker_bps=10.0, slippage_bps=5.0, min_net_bps=10.0)
    # okx bid 50500 vs binance ask 50000 = ~99 bps gross, 30 fees -> ~69 net
    books = {
        "binance": {"bid": 49990.0, "ask": 50000.0},
        "okx": {"bid": 50500.0, "ask": 50510.0},
    }
    findings = s.evaluate(books)
    assert len(findings) == 1
    f = findings[0]
    assert f.venues == ("binance", "okx")
    assert f.net_edge_bps > 50


def test_spread_below_fees_suppressed() -> None:
    s = SpreadScanner(taker_bps=10.0, slippage_bps=5.0, min_net_bps=10.0)
    # 10 bps gross < 30 bps fees -> nothing
    books = {
        "binance": {"bid": 49999.0, "ask": 50000.0},
        "okx": {"bid": 50050.0, "ask": 50060.0},
    }
    assert s.evaluate(books) == []


# ---- BasisScanner ----------------------------------------------------------

def test_basis_window_detected() -> None:
    s = BasisScanner(min_basis_apr=0.08)
    # 3% over spot with 90d to expiry = ~12% APR
    findings = s.evaluate(spot=50_000.0, future=51_500.0, days_to_expiry=90)
    assert len(findings) == 1
    assert findings[0].kind == "basis"
    assert findings[0].confidence >= 0.8


def test_basis_thin_ignored() -> None:
    s = BasisScanner(min_basis_apr=0.08)
    # 0.5% over 90d = ~2% APR
    assert s.evaluate(spot=50_000.0, future=50_250.0, days_to_expiry=90) == []


# ---- DepegScanner ----------------------------------------------------------

def test_depeg_detected() -> None:
    s = DepegScanner(min_deviation_bps=30.0)
    findings = s.evaluate({"USDC": 0.97})  # 300 bps below peg
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "depeg"
    assert "HUMAN DECISION" in f.action_hint


def test_stable_at_peg_quiet() -> None:
    s = DepegScanner(min_deviation_bps=30.0)
    assert s.evaluate({"USDC": 0.9999, "DAI": 1.0002}) == []


# ---- WickScanner -----------------------------------------------------------

def _crash_ohlcv() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=48, freq="1h", tz="UTC")
    close = pd.Series(50_000.0, index=idx)
    close.iloc[-4:] = [50_000, 47_000, 45_000, 44_000]  # -12% in 4 bars
    low = close * 0.99
    low.iloc[-3:] = close.iloc[-3:] * 0.93               # long lower wicks
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.002,
        "low": low,
        "close": close,
        "volume": 100.0,
    }, index=idx)


def test_wick_regime_fires_on_cascade() -> None:
    s = WickScanner(flash_crash_pct=0.05, flash_crash_bars=4)
    findings = s.evaluate(_crash_ohlcv())
    assert len(findings) == 1
    assert findings[0].kind == "wick_regime"
    assert findings[0].confidence >= 0.6


def test_wick_regime_quiet_on_calm() -> None:
    idx = pd.date_range("2024-01-01", periods=48, freq="1h", tz="UTC")
    close = pd.Series(np.linspace(50_000, 50_500, 48), index=idx)
    open_ = close.shift(1).fillna(close.iloc[0] - 10)  # real candle bodies
    ohlcv = pd.DataFrame({
        "open": open_,
        "high": pd.concat([open_, close], axis=1).max(axis=1) + 1.0,
        "low": pd.concat([open_, close], axis=1).min(axis=1) - 1.0,
        "close": close, "volume": 100.0,
    }, index=idx)
    assert WickScanner().evaluate(ohlcv) == []


# ---- Ranker ----------------------------------------------------------------

def test_ranker_orders_by_score_and_persists(tmp_path: Path) -> None:
    fund = FundingScanner(min_carry_apr=0.10)
    dep = DepegScanner()
    findings = fund.evaluate({"binance": 0.0005}) + dep.evaluate({"USDC": 0.995})
    ranked = rank_findings(findings)
    assert ranked[0].score >= ranked[-1].score
    write_findings(ranked, runtime_dir=tmp_path)
    loaded = read_findings(runtime_dir=tmp_path)
    assert len(loaded) == len(ranked)
    assert loaded[0]["kind"] == ranked[0].kind


# ---- IntelDiscovery strategy (evaluate path, injected data) -----------------

def test_intel_strategy_emits_watch_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # write_findings goes under tmp data/runtime
    cfg = StrategyConfig(name="intel", params={"alert_min_net_bps": 50.0})
    strat = IntelDiscovery(cfg)
    data = {
        "funding_rates": {"binance": 0.0005},  # 54.75% APR — fat
        "tickers": {},
        "stable_prices": {},
        "ohlcv": None,
    }
    result = strat.evaluate(data)
    assert result.opportunity is not None
    assert result.opportunity.action == "watch"  # never buy/sell
    assert result.metrics["findings"] >= 1
    assert (tmp_path / "data" / "runtime" / "intel" / "opportunities.json").exists()


def test_intel_strategy_quiet_when_no_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = StrategyConfig(name="intel")
    strat = IntelDiscovery(cfg)
    result = strat.evaluate({"funding_rates": {}, "tickers": {}, "stable_prices": {}, "ohlcv": None})
    assert result.opportunity is None
    assert result.metrics["findings"] == 0
