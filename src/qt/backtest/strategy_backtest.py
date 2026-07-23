"""Unified backtest entry point for the solution-gallery strategies.

One function — :func:`run_strategy_backtest` — takes a strategy id, a
price history, and optional auxiliary series, dispatches to the right
batch backtester in :mod:`qt.strategies.sim`, and returns a normalized
:class:`BacktestOutcome` (equity curve + trades + standard metrics).

It also ships :func:`synthetic_btc_ohlcv` so a backtest can always run —
even with no network and no local parquet — which matters in locked-down
environments where exchange APIs are unreachable. Synthetic data is
clearly labeled as such in the outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from qt.backtest.metrics import Metrics, compute_metrics
from qt.backtest.validation import ohlcv_fingerprint, validate_ohlcv
from qt.strategies.sim import (
    BasisCarryBacktest,
    BasisCarryConfig,
    SmartDCABacktest,
    SmartDCAConfig,
    WeeklyTrendBacktest,
    WeeklyTrendConfig,
    WickCatcherBacktest,
    WickCatcherConfig,
)

# Canonical strategy ids and their aliases.
STRATEGY_ALIASES: dict[str, str] = {
    "a": "dca", "dca": "dca", "smart_dca": "dca",
    "c": "trend", "trend": "trend", "weekly": "trend", "weekly_trend": "trend",
    "d": "carry", "carry": "carry", "basis": "carry", "basis_carry": "carry",
    "e": "wick", "wick": "wick", "wick_catcher": "wick",
}

SUPPORTED = ("dca", "trend", "carry", "wick")


@dataclass
class BacktestOutcome:
    """Normalized result of a strategy backtest."""

    strategy: str
    equity: pd.Series
    trades: pd.DataFrame
    metrics: Metrics
    synthetic: bool = False
    diagnostics: pd.DataFrame = field(default_factory=pd.DataFrame)
    data_fingerprint: str = ""

    def summary(self) -> dict[str, Any]:
        eq = self.equity
        return {
            "strategy": self.strategy,
            "synthetic": self.synthetic,
            "bars": len(eq),
            "start": eq.index[0].isoformat() if len(eq) else None,
            "end": eq.index[-1].isoformat() if len(eq) else None,
            "initial_equity": float(eq.iloc[0]) if len(eq) else 0.0,
            "final_equity": float(eq.iloc[-1]) if len(eq) else 0.0,
            "x_multiple": float(eq.iloc[-1] / eq.iloc[0]) if len(eq) and eq.iloc[0] > 0 else 0.0,
            "num_trades": int(self.metrics.num_trades),
            "data_fingerprint": self.data_fingerprint,
            "metrics": {
                "total_return": self.metrics.total_return,
                "cagr": self.metrics.cagr,
                "sharpe": self.metrics.sharpe,
                "sortino": self.metrics.sortino,
                "calmar": self.metrics.calmar,
                "max_drawdown": self.metrics.max_drawdown,
                "win_rate": self.metrics.win_rate,
                "profit_factor": self.metrics.profit_factor,
            },
        }


def canonical_strategy(which: str) -> str:
    """Normalize a strategy id/alias to its canonical form. Raises on unknown."""
    key = which.strip().lower()
    if key not in STRATEGY_ALIASES:
        raise ValueError(
            f"unknown strategy {which!r}; supported: {', '.join(SUPPORTED)} "
            f"(capitulation uses the live `qt backtest` engine, not this batch path)"
        )
    return STRATEGY_ALIASES[key]


def synthetic_btc_ohlcv(
    *,
    days: int = 730,
    freq: str = "1h",
    start: str = "2022-01-01",
    seed: int = 7,
    start_price: float = 30_000.0,
) -> pd.DataFrame:
    """Generate a deterministic synthetic BTC OHLCV history.

    A geometric random walk with mild upward drift, a couple of injected
    crash/recovery cycles, and realistic intrabar wicks. Deterministic
    given ``seed`` so backtests are reproducible. NOT market data — for
    smoke-testing the pipeline and demos only.
    """

    rng = np.random.default_rng(seed)
    periods = days * (24 if freq == "1h" else 1)
    idx = pd.date_range(start=start, periods=periods, freq=freq, tz="UTC")

    # Base drift + noise (per-bar). ~35% annual drift, ~55% annual vol.
    bars_per_year = 365 * (24 if freq == "1h" else 1)
    mu = 0.35 / bars_per_year
    sigma = 0.55 / np.sqrt(bars_per_year)
    shocks = rng.normal(mu, sigma, size=periods)

    # Inject two crash+recovery cycles at 1/3 and 2/3. Each crash is a total
    # ~45% drawdown spread over `span` bars, followed by a ~40% recovery over
    # the next span — scaled by span so it never compounds to zero.
    span = max(24, periods // 30)
    for frac in (0.33, 0.66):
        c = int(periods * frac)
        crash_end = min(c + span, periods)
        rec_end = min(crash_end + span, periods)
        # log(0.55) ≈ -0.60 total drawdown over the crash window
        shocks[c:crash_end] += np.log(0.55) / span
        # log(1.40) ≈ +0.34 recovery over the recovery window
        shocks[crash_end:rec_end] += np.log(1.40) / span

    log_price = np.log(start_price) + np.cumsum(shocks)
    close = np.exp(log_price)

    # Build OHLC around close with wicks.
    open_ = np.empty(periods)
    open_[0] = start_price
    open_[1:] = close[:-1]
    wick = np.abs(rng.normal(0, sigma, size=periods)) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    volume = rng.uniform(50, 500, size=periods) * (1 + np.abs(shocks) * 20)

    # Deterministic lower-tail stress events so offline smoke tests exercise
    # wick-ladder strategies instead of producing a no-trade path.
    for frac in (0.18, 0.42, 0.58, 0.78):
        i = int(periods * frac)
        if 1 <= i < periods - 1:
            base = close[i - 1]
            low[i] = min(low[i], base * 0.86)
            high[i + 1] = max(high[i + 1], base * 0.99)
            volume[i] *= 4.0

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def synthetic_funding(index: pd.DatetimeIndex, *, seed: int = 11) -> pd.Series:
    """Deterministic synthetic 8h funding-rate series aligned to ``index``.

    Mean-positive (~ +0.01%/8h) with occasional negative stretches, which
    is what makes basis carry profitable most of the time.
    """
    rng = np.random.default_rng(seed)
    base = rng.normal(0.0001, 0.0004, size=len(index))
    # A couple of negative-funding regimes.
    n = len(index)
    for frac in (0.25, 0.7):
        c = int(n * frac)
        span = max(1, n // 20)
        base[c:c + span] -= 0.0006
    return pd.Series(base, index=index, name="funding_rate")


def run_strategy_backtest(
    which: str,
    ohlcv: pd.DataFrame | None = None,
    *,
    initial_cash: float = 10_000.0,
    funding: pd.Series | None = None,
    fear_greed: pd.Series | None = None,
    mvrv_z: pd.Series | None = None,
    nupl: pd.Series | None = None,
    allow_synthetic: bool = True,
    synthetic_days: int = 730,
) -> BacktestOutcome:
    """Run a batch backtest of one gallery strategy.

    If ``ohlcv`` is None/empty and ``allow_synthetic`` is True, a
    deterministic synthetic history is generated so the backtest always
    produces output. Carry additionally synthesizes a funding series when
    none is supplied.
    """

    strat = canonical_strategy(which)

    synthetic = False
    if ohlcv is None or ohlcv.empty:
        if not allow_synthetic:
            raise ValueError(f"no OHLCV data for {strat} and allow_synthetic=False")
        ohlcv = synthetic_btc_ohlcv(days=synthetic_days)
        synthetic = True
    ohlcv = validate_ohlcv(ohlcv)
    data_fingerprint = ohlcv_fingerprint(ohlcv)

    close = ohlcv["close"]

    if strat == "dca":
        result = SmartDCABacktest(SmartDCAConfig(initial_cash=initial_cash)).run(
            ohlcv, fear_greed=fear_greed, mvrv_z=mvrv_z, nupl=nupl,
        )
    elif strat == "trend":
        result = WeeklyTrendBacktest(WeeklyTrendConfig(initial_cash=initial_cash)).run(ohlcv)
    elif strat == "carry":
        if funding is None or (hasattr(funding, "empty") and funding.empty):
            if not allow_synthetic:
                raise ValueError("basis carry requires funding-rate history")
            funding = synthetic_funding(close.index)
            synthetic = True
        result = BasisCarryBacktest(BasisCarryConfig(initial_cash=initial_cash)).run(
            ohlcv, funding=funding,
        )
    elif strat == "wick":
        result = WickCatcherBacktest(
            WickCatcherConfig(initial_cash=initial_cash)
        ).run(ohlcv)
    else:  # pragma: no cover - guarded by canonical_strategy
        raise ValueError(f"unsupported strategy {strat}")

    equity = result.equity
    trades = _normalize_trades(result.trades)
    metrics = compute_metrics(equity, trades)

    return BacktestOutcome(
        strategy=strat,
        equity=equity,
        trades=trades,
        metrics=metrics,
        synthetic=synthetic,
        diagnostics=getattr(result, "diagnostics", pd.DataFrame()),
        data_fingerprint=data_fingerprint,
    )


def write_strategy_backtest_artifacts(
    outcome: BacktestOutcome,
    output_dir: str | Path,
) -> Path:
    """Export equity.csv, trades.csv, summary.json under output_dir/strategy_<name>_<ts>/.

    Also refreshes ``strategy_latest.json`` at the root so the dashboard can
    surface the most recent strategy backtest. Returns the run directory.
    """
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / f"strategy_{outcome.strategy}_{ts}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = root / f"strategy_{outcome.strategy}_{ts}-{suffix}"
    run_dir.mkdir(parents=True)

    outcome.equity.to_frame("equity").to_csv(run_dir / "equity.csv", index_label="ts")
    outcome.trades.to_csv(run_dir / "trades.csv", index=False)

    summary = outcome.summary()
    summary["run_id"] = run_dir.name
    summary["created_at"] = datetime.now(tz=timezone.utc).isoformat()
    summary["files"] = {
        "equity": str(run_dir / "equity.csv"),
        "trades": str(run_dir / "trades.csv"),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (root / "strategy_latest.json").write_text(json.dumps(summary, indent=2, default=str))
    return run_dir


def _normalize_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Ensure a ``pnl`` column exists so compute_metrics can run.

    The gallery simulators emit per-fill rows (buy/sell/short) rather than
    round-trip trades with realized pnl. We approximate per-trade pnl as
    zero here (the metrics that matter for these strategies — total return,
    CAGR, max DD, Sharpe — come from the equity curve, not per-trade pnl).
    Win-rate / profit-factor are only meaningful for the event engine.
    """
    if trades is None or trades.empty:
        return pd.DataFrame(columns=["ts", "side", "qty", "price", "fee", "pnl"])
    df = trades.copy()
    if "pnl" not in df.columns:
        df["pnl"] = 0.0
    return df
