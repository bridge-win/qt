"""Walk-forward analysis: rolling train / OOS test windows.

Walk-forward is the practitioner-standard guardrail against curve-fitting.
We split history into overlapping (train, test) windows; on each train
window we run a grid/random search over `ThresholdConfig` knobs, pick the
best by `selector_metric`, then evaluate the selected config on the
immediately-following test window. Aggregating OOS test windows gives an
unbiased estimate of how the strategy would have performed live across
regimes.

References:
- Pardo (2008), *The Evaluation and Optimization of Trading Strategies*.
- Bailey, Borwein, López de Prado, Zhu (2014), "The Probability of
  Backtest Overfitting" (Journal of Computational Finance) — basis for
  the deflated Sharpe ratio computation in `metrics.py`.
- López de Prado (2018), *Advances in Financial Machine Learning*, ch. 7.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from qt.backtest.engine import Backtester, BacktestResult
from qt.backtest.metrics import Metrics
from qt.core.config import RiskConfig, ThresholdConfig
from qt.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class WalkForwardWindow:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    chosen_thresholds: ThresholdConfig | None = None
    train_metric: float = float("nan")
    test_result: BacktestResult | None = None


@dataclass
class WalkForwardResult:
    windows: list[WalkForwardWindow]
    combined_equity: pd.Series
    combined_trades: pd.DataFrame
    oos_metrics: Metrics

    def stability_ratio(self) -> float:
        """Std(test Sharpe) / mean(test Sharpe). Lower is more stable."""

        sharpes = []
        for w in self.windows:
            if w.test_result is not None:
                sharpes.append(w.test_result.metrics.sharpe)
        if not sharpes:
            return float("nan")
        s = np.array(sharpes)
        denom = abs(s.mean())
        return float(s.std(ddof=0) / denom) if denom > 1e-9 else float("nan")


def generate_windows(
    index: pd.DatetimeIndex,
    train_days: int = 730,
    test_days: int = 180,
    step_days: int = 90,
) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """Generate (train_start, train_end, test_start, test_end) tuples."""

    if len(index) == 0:
        return []
    start = index[0]
    end = index[-1]
    out: list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]] = []
    cur = start
    while True:
        train_start = cur
        train_end = train_start + timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        out.append((train_start, train_end, test_start, test_end))
        cur = cur + timedelta(days=step_days)
    return out


def default_threshold_grid() -> list[dict]:
    """Conservative-to-aggressive grid over a handful of knobs."""

    return [
        {"entry_score_min": ems, "min_factor_groups": mfg,
         "rsi_oversold": rsi, "drawdown_30d_min": dd}
        for ems, mfg, rsi, dd in itertools.product(
            [0.45, 0.55, 0.65],         # entry score
            [2, 3, 4],                  # min groups
            [20, 25, 30],               # RSI threshold
            [0.10, 0.15, 0.20],         # drawdown threshold
        )
    ]


def _slice_inputs(
    ohlcv: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    aux: dict[str, pd.Series | None],
) -> tuple[pd.DataFrame, dict[str, pd.Series | None]]:
    sub = ohlcv.loc[start:end]
    aux_sub: dict[str, pd.Series | None] = {}
    for k, v in aux.items():
        if v is None or v.empty:
            aux_sub[k] = None
            continue
        s = v.loc[start:end]
        aux_sub[k] = s if not s.empty else None
    return sub, aux_sub


def run_walk_forward(
    ohlcv: pd.DataFrame,
    aux_inputs: dict[str, pd.Series | None],
    base_thresholds: ThresholdConfig,
    risk_cfg: RiskConfig,
    *,
    initial_cash: float = 100_000.0,
    train_days: int = 730,
    test_days: int = 180,
    step_days: int = 90,
    grid: list[dict] | None = None,
    selector: Callable[[Metrics], float] | None = None,
    min_trades_to_select: int = 3,
) -> WalkForwardResult:
    """Execute walk-forward with a grid search.

    `aux_inputs`: dict of named optional series forwarded to `Backtester.run`.
    `selector`: scalar score from Metrics; default = Sharpe / max(1, 1-num_trades-penalty).
    """

    grid = grid or default_threshold_grid()
    if selector is None:
        selector = lambda m: m.sharpe if m.num_trades >= min_trades_to_select else -1e9

    windows = generate_windows(ohlcv.index, train_days, test_days, step_days)
    if not windows:
        raise ValueError("Not enough history for walk-forward; reduce train/test/step days.")

    result_windows: list[WalkForwardWindow] = []
    combined_equity_parts: list[pd.Series] = []
    combined_trades_parts: list[pd.DataFrame] = []

    for (ts, te, vs, ve) in windows:
        train_ohlcv, train_aux = _slice_inputs(ohlcv, ts, te, aux_inputs)
        test_ohlcv, test_aux = _slice_inputs(ohlcv, vs, ve, aux_inputs)
        if train_ohlcv.empty or test_ohlcv.empty:
            continue
        best_thresh: ThresholdConfig | None = None
        best_score = -float("inf")
        for params in grid:
            cand = base_thresholds.model_copy(update=params)
            bt = Backtester(thresholds=cand, risk_cfg=risk_cfg, initial_cash=initial_cash)
            try:
                res = bt.run(ohlcv=train_ohlcv, **train_aux)
            except Exception as e:
                log.warning("walkforward_train_err", err=str(e), params=params)
                continue
            score = selector(res.metrics)
            if score > best_score:
                best_score = score
                best_thresh = cand

        win = WalkForwardWindow(
            train_start=ts, train_end=te, test_start=vs, test_end=ve,
            chosen_thresholds=best_thresh, train_metric=best_score,
        )
        if best_thresh is not None:
            bt = Backtester(thresholds=best_thresh, risk_cfg=risk_cfg,
                            initial_cash=initial_cash)
            try:
                test_res = bt.run(ohlcv=test_ohlcv, **test_aux)
                win.test_result = test_res
                combined_equity_parts.append(test_res.equity_curve)
                if not test_res.trades.empty:
                    combined_trades_parts.append(test_res.trades)
            except Exception as e:
                log.warning("walkforward_test_err", err=str(e))
        result_windows.append(win)

    # Combine OOS curves into one equity timeline by chaining returns.
    if combined_equity_parts:
        # Each test window starts at `initial_cash`; chain by relative returns.
        chained = combined_equity_parts[0].copy()
        last = chained.iloc[-1]
        for part in combined_equity_parts[1:]:
            ret = part / part.iloc[0]
            chained = pd.concat([chained, ret * last])
            last = chained.iloc[-1]
        combined_equity = chained
    else:
        combined_equity = pd.Series(dtype="float64")

    combined_trades = (
        pd.concat(combined_trades_parts, ignore_index=True)
        if combined_trades_parts else pd.DataFrame()
    )
    from qt.backtest.metrics import compute_metrics
    oos_metrics = compute_metrics(combined_equity, combined_trades)
    return WalkForwardResult(
        windows=result_windows,
        combined_equity=combined_equity,
        combined_trades=combined_trades,
        oos_metrics=oos_metrics,
    )
