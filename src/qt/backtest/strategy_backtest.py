"""Unified backtest entry point for the solution-gallery strategies.

One function — :func:`run_strategy_backtest` — takes a strategy id, a
price history, and optional auxiliary series, dispatches to the independent
``btc_backtest`` event engine, and returns a normalized :class:`BacktestOutcome`
(equity curve + trades + standard metrics).

It also ships :func:`synthetic_btc_ohlcv` so a backtest can always run —
even with no network and no local parquet — which matters in locked-down
environments where exchange APIs are unreachable. Synthetic data is
clearly labeled as such in the outcome.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    DataSegment,
    MarketBundle,
    MarketDataset,
    Timeframe,
)
from btc_backtest.engine.models import BacktestResult, BacktestSpec
from btc_backtest.engine.runner import EventRunner
from btc_backtest.strategies.registry import default_strategy_registry

from qt.backtest.metrics import Metrics, compute_metrics
from qt.backtest.validation import ohlcv_fingerprint, validate_ohlcv

# Canonical strategy ids and their aliases.
STRATEGY_ALIASES: dict[str, str] = {
    "a": "dca", "dca": "dca", "smart_dca": "dca",
    "c": "trend", "trend": "trend", "weekly": "trend", "weekly_trend": "trend",
    "sma_crossover": "trend",
    "d": "carry", "carry": "carry", "basis": "carry", "basis_carry": "carry",
    "funding_basis_carry": "carry",
    "e": "wick", "wick": "wick", "wick_catcher": "wick",
}

SUPPORTED = ("dca", "trend", "carry", "wick")
ENGINE_STRATEGIES: dict[str, str] = {
    "dca": "smart_dca",
    "trend": "sma_crossover",
    "carry": "funding_basis_carry",
    "wick": "wick_catcher",
}


@dataclass
class BacktestOutcome:
    """Normalized result of a strategy backtest."""

    strategy: str
    engine_strategy: str
    equity: pd.Series
    trades: pd.DataFrame
    metrics: Metrics
    synthetic: bool = False
    diagnostics: pd.DataFrame = field(default_factory=pd.DataFrame)
    data_fingerprint: str = ""
    engine_run_id: str = ""

    def summary(self) -> dict[str, object]:
        eq = self.equity
        return {
            "strategy": self.strategy,
            "engine_strategy": self.engine_strategy,
            "synthetic": self.synthetic,
            "bars": len(eq),
            "start": eq.index[0].isoformat() if len(eq) else None,
            "end": eq.index[-1].isoformat() if len(eq) else None,
            "initial_equity": float(eq.iloc[0]) if len(eq) else 0.0,
            "final_equity": float(eq.iloc[-1]) if len(eq) else 0.0,
            "x_multiple": float(eq.iloc[-1] / eq.iloc[0]) if len(eq) and eq.iloc[0] > 0 else 0.0,
            "num_trades": int(self.metrics.num_trades),
            "data_fingerprint": self.data_fingerprint,
            "engine_run_id": self.engine_run_id,
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
    frequency = "1h" if freq == "1h" else "1D"
    idx = pd.date_range(start=start, periods=periods, freq=frequency, tz="UTC")

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
    engine_strategy = ENGINE_STRATEGIES[strat]

    synthetic = False
    ohlcv_synthetic = False
    funding_synthetic = False
    if ohlcv is None or ohlcv.empty:
        if not allow_synthetic:
            raise ValueError(f"no OHLCV data for {strat} and allow_synthetic=False")
        ohlcv = synthetic_btc_ohlcv(days=synthetic_days)
        synthetic = True
        ohlcv_synthetic = True
    ohlcv = validate_ohlcv(ohlcv)
    data_fingerprint = ohlcv_fingerprint(ohlcv)

    if (
        strat == "carry"
        and allow_synthetic
        and (funding is None or (hasattr(funding, "empty") and funding.empty))
    ):
        funding = synthetic_funding(ohlcv.index)
        synthetic = True
        funding_synthetic = True

    spec = _spec(
        engine_strategy,
        ohlcv,
        initial_cash=initial_cash,
        synthetic=ohlcv_synthetic,
    )
    auxiliary = _auxiliary_datasets(
        strategy=strat,
        ohlcv=ohlcv,
        funding=funding,
        fear_greed=fear_greed,
        mvrv_z=mvrv_z,
        nupl=nupl,
        ohlcv_synthetic=ohlcv_synthetic,
        funding_synthetic=funding_synthetic,
        request=spec.data,
    )
    registry = default_strategy_registry()
    result = EventRunner().run(
        spec,
        MarketBundle(
            primary=_market_dataset(
                "spot",
                ohlcv,
                request=spec.data,
                real_data=not ohlcv_synthetic,
                source="qt-memory://ohlcv",
            ),
            auxiliary=auxiliary,
        ),
        registry.create(engine_strategy, {}),
    )

    equity = _equity_series(result)
    trades = _trades_frame(result)
    metrics = compute_metrics(equity, trades)

    return BacktestOutcome(
        strategy=strat,
        engine_strategy=engine_strategy,
        equity=equity,
        trades=trades,
        metrics=metrics,
        synthetic=synthetic,
        diagnostics=_diagnostics_frame(result),
        data_fingerprint=data_fingerprint,
        engine_run_id=result.run_id,
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


def _spec(
    strategy: str,
    ohlcv: pd.DataFrame,
    *,
    initial_cash: float,
    synthetic: bool,
) -> BacktestSpec:
    timeframe = _infer_timeframe(ohlcv.index)
    request = _request_for(ohlcv, timeframe=timeframe, synthetic=synthetic)
    return BacktestSpec(
        strategy=strategy,
        data=request,
        initial_cash=Decimal(str(initial_cash)),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


def _infer_timeframe(index: pd.DatetimeIndex) -> Timeframe:
    if len(index) < 2:
        return "1h"
    deltas = index.to_series().diff().dropna()
    if deltas.empty:
        return "1h"
    median = deltas.median()
    return "1h" if median <= pd.Timedelta(hours=1) else "1d"


def _bar_delta(timeframe: Timeframe) -> timedelta:
    return timedelta(hours=1) if timeframe == "1h" else timedelta(days=1)


def _request_for(
    frame: pd.DataFrame,
    *,
    timeframe: Timeframe,
    synthetic: bool,
    market: str = "spot",
) -> DataRequest:
    index = frame.index
    start = pd.Timestamp(index[0]).to_pydatetime()
    end = (pd.Timestamp(index[-1]) + _bar_delta(timeframe)).to_pydatetime()
    return DataRequest(
        provider="qt-memory",
        symbol="BTC/USD",
        timeframe=timeframe,
        start=start,
        end=end,
        market=market,
        require_real=not synthetic,
    )


def _market_dataset(
    market: str,
    frame: pd.DataFrame,
    *,
    request: DataRequest,
    real_data: bool,
    source: str,
) -> MarketDataset:
    return MarketDataset(
        frame=frame.copy(),
        manifest=_manifest(
            market=market,
            frame=frame,
            request=request,
            real_data=real_data,
            source=source,
        ),
    )


def _manifest(
    *,
    market: str,
    frame: pd.DataFrame,
    request: DataRequest,
    real_data: bool,
    source: str,
) -> DataManifest:
    normalized_sha256 = _frame_digest(frame)
    delivered_start = pd.Timestamp(frame.index[0]).to_pydatetime()
    delivered_end = (
        pd.Timestamp(frame.index[-1]) + _bar_delta(request.timeframe)
    ).to_pydatetime()
    return DataManifest(
        provider=request.provider,
        market=market,
        symbol=request.symbol,
        timeframe=request.timeframe,
        requested_start=request.start,
        requested_end=request.end,
        delivered_start=delivered_start,
        delivered_end=delivered_end,
        retrieved_at=datetime.now(tz=timezone.utc),
        real_data=real_data,
        raw_sha256=(normalized_sha256,),
        normalized_sha256=normalized_sha256,
        source=source,
        segments=(
            DataSegment(
                provider=request.provider,
                market=market,
                symbol=request.symbol,
                timeframe=request.timeframe,
                start=delivered_start,
                end=delivered_end,
                real_data=real_data,
                normalized_sha256=normalized_sha256,
                source=source,
            ),
        ),
    )


def _frame_digest(frame: pd.DataFrame) -> str:
    digest_frame = frame.copy()
    digest_frame.index = digest_frame.index.tz_convert("UTC")
    payload = digest_frame.to_csv(
        index=True,
        index_label="timestamp",
        date_format="%Y-%m-%dT%H:%M:%S.%fZ",
        float_format="%.12g",
        lineterminator="\n",
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _auxiliary_datasets(
    *,
    strategy: str,
    ohlcv: pd.DataFrame,
    funding: pd.Series | None,
    fear_greed: pd.Series | None,
    mvrv_z: pd.Series | None,
    nupl: pd.Series | None,
    ohlcv_synthetic: bool,
    funding_synthetic: bool,
    request: DataRequest,
) -> dict[str, MarketDataset]:
    auxiliary: dict[str, MarketDataset] = {}
    if strategy == "carry":
        auxiliary["perpetual"] = _market_dataset(
            "perpetual",
            ohlcv,
            request=request.model_copy(update={"market": "perpetual"}),
            real_data=not ohlcv_synthetic,
            source="qt-memory://perpetual",
        )
        if funding is not None and not funding.empty:
            funding_frame = _series_frame("rate", funding, ohlcv.index)
            if not funding_frame.empty:
                auxiliary["funding"] = _market_dataset(
                    "funding",
                    funding_frame,
                    request=request.model_copy(update={"market": "funding"}),
                    real_data=not funding_synthetic,
                    source="qt-memory://funding",
                )
    if strategy == "dca":
        features: dict[str, pd.Series] = {}
        if fear_greed is not None and not fear_greed.empty:
            features["fear_greed"] = _aligned_series(fear_greed, ohlcv.index)
        if mvrv_z is not None and not mvrv_z.empty:
            features["valuation_zscore"] = _aligned_series(mvrv_z, ohlcv.index)
        if nupl is not None and not nupl.empty:
            features["nupl"] = _aligned_series(nupl, ohlcv.index)
        if features:
            feature_frame = pd.DataFrame(features).dropna(how="all")
            if not feature_frame.empty:
                auxiliary["features"] = _market_dataset(
                    "features",
                    feature_frame,
                    request=request.model_copy(update={"market": "features"}),
                    real_data=True,
                    source="qt-memory://features",
                )
    return auxiliary


def _series_frame(
    column: str,
    series: pd.Series,
    index: pd.DatetimeIndex,
) -> pd.DataFrame:
    aligned = _aligned_series(series, index)
    return pd.DataFrame({column: aligned}).dropna()


def _aligned_series(series: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    aligned = pd.to_numeric(series.copy(), errors="coerce")
    if not isinstance(aligned.index, pd.DatetimeIndex):
        raise ValueError("auxiliary series index must be a DatetimeIndex")
    if aligned.index.tz is None:
        raise ValueError("auxiliary series index must be timezone-aware UTC")
    aligned.index = aligned.index.tz_convert("UTC")
    aligned = aligned.sort_index()
    return aligned.reindex(index).ffill()


def _equity_series(result: BacktestResult) -> pd.Series:
    values = [float(snapshot.equity) for snapshot in result.snapshots]
    index = pd.DatetimeIndex([snapshot.timestamp for snapshot in result.snapshots])
    return pd.Series(values, index=index, name="equity")


def _trades_frame(result: BacktestResult) -> pd.DataFrame:
    if result.trades:
        return pd.DataFrame(
            {
                "ts": trade.closed_at,
                "side": "sell",
                "qty": float(trade.quantity),
                "price": float(trade.exit_price),
                "fee": float(trade.fees),
                "pnl": float(trade.realized_pnl),
            }
            for trade in result.trades
        )
    return _normalize_trades(
        pd.DataFrame(
            {
                "ts": fill.timestamp,
                "side": fill.side.value,
                "qty": float(fill.quantity),
                "price": float(fill.price),
                "fee": float(fill.fee),
                "pnl": 0.0,
            }
            for fill in result.fills
        )
    )


def _diagnostics_frame(result: BacktestResult) -> pd.DataFrame:
    diagnostics = dict(result.diagnostics)
    diagnostics["warnings"] = list(result.warnings)
    return pd.DataFrame([diagnostics])
