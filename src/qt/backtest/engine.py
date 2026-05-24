"""Event-driven backtester.

Iterates over OHLCV bars; on each bar:
  1. Mark-to-market existing positions, check exits (stop/TP/time).
  2. Evaluate the signal engine on data up to and including the bar.
  3. Ask the risk engine for an entry decision.
  4. Simulate the fill (fees + slippage from `FillModel`).
  5. Record equity, trades, signals for later reporting.

This is intentionally a single-symbol long-only engine — matching the
strategy scope (BTC mean-reversion buying on extremes). Extending to
multi-symbol portfolio backtests is left to a future milestone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from qt.backtest.fills import FillModel
from qt.backtest.metrics import Metrics, compute_metrics
from qt.core.config import RiskConfig, ThresholdConfig
from qt.core.logging import get_logger
from qt.core.types import OrderSide, Position, Trade
from qt.indicators.price import atr
from qt.indicators.volatility import realized_vol
from qt.risk.engine import RiskEngine
from qt.signal.engine import SignalEngine

log = get_logger(__name__)


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: pd.DataFrame
    signals: pd.DataFrame
    metrics: Metrics


@dataclass
class Backtester:
    thresholds: ThresholdConfig
    risk_cfg: RiskConfig
    fills: FillModel = field(default_factory=FillModel)
    initial_cash: float = 100_000.0
    bar_seconds: int = 3600

    def run(
        self,
        ohlcv: pd.DataFrame,
        *,
        funding: pd.Series | None = None,
        oi: pd.Series | None = None,
        long_short_ratio: pd.Series | None = None,
        sopr: pd.Series | None = None,
        mvrv_z: pd.Series | None = None,
        nupl: pd.Series | None = None,
        puell: pd.Series | None = None,
        reserve_risk: pd.Series | None = None,
        exchange_netflow: pd.Series | None = None,
        fear_greed: pd.Series | None = None,
        social_sentiment: pd.Series | None = None,
        vix: pd.Series | None = None,
        dxy: pd.Series | None = None,
    ) -> BacktestResult:
        if ohlcv.empty:
            raise ValueError("ohlcv is empty")

        sig_engine = SignalEngine(thresholds=self.thresholds)
        risk_engine = RiskEngine(cfg=self.risk_cfg, bar_seconds=self.bar_seconds)
        risk_engine.state.equity_peak = self.initial_cash

        # Pre-compute the score for the whole history — backtester reads it
        # one bar at a time, but the inputs are all point-in-time already
        # (no future leakage from rolling windows because they only look back).
        score = sig_engine.evaluate(
            ohlcv=ohlcv,
            funding=funding,
            oi=oi,
            long_short_ratio=long_short_ratio,
            sopr=sopr,
            mvrv_z=mvrv_z,
            nupl=nupl,
            puell=puell,
            reserve_risk=reserve_risk,
            exchange_netflow=exchange_netflow,
            fear_greed=fear_greed,
            social_sentiment=social_sentiment,
            vix=vix,
            dxy=dxy,
        )
        signals = sig_engine.to_signals(score)
        signals_by_ts = {s.ts: s for s in signals}

        atr_series = atr(ohlcv["high"], ohlcv["low"], ohlcv["close"], 14)
        rv_series = realized_vol(ohlcv["close"], window=24 * 7,
                                 annualize_factor=365 * 24)

        cash = self.initial_cash
        position = Position(symbol="BTC/USDT")
        equity_records: list[tuple[datetime, float]] = []
        trade_records: list[dict[str, object]] = []
        entry_ts: datetime | None = None
        entry_price: float = 0.0

        for ts, row in ohlcv.iterrows():
            ts_py: datetime = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            mark = float(row["close"])

            # 1. Exit check
            if not position.is_flat:
                exit_dec = risk_engine.evaluate_exit(position, mark, ts_py)
                if exit_dec.action == "close":
                    fill_px = self.fills.fill_price(mark, OrderSide.SELL)
                    notional = position.qty * fill_px
                    fee = self.fills.fee(notional)
                    pnl = (fill_px - position.avg_price) * position.qty - fee
                    cash += notional - fee
                    trade_records.append(
                        {
                            "entry_ts": entry_ts,
                            "exit_ts": ts_py,
                            "entry_price": entry_price,
                            "exit_price": fill_px,
                            "qty": position.qty,
                            "pnl": pnl,
                            "return_pct": (fill_px / position.avg_price) - 1,
                            "reason": exit_dec.reason,
                            "holding_bars": (
                                (ts_py - entry_ts).total_seconds() / self.bar_seconds
                                if entry_ts else 0
                            ),
                        }
                    )
                    risk_engine.record_realized(pnl, ts_py)
                    position = Position(symbol="BTC/USDT")
                    entry_ts = None
                    entry_price = 0.0

            # 2. Entry check
            sig = signals_by_ts.get(ts_py)
            if sig is not None and position.is_flat:
                atr_val = float(atr_series.loc[ts]) if pd.notna(atr_series.loc[ts]) else mark * 0.02
                rv_val = float(rv_series.loc[ts]) if pd.notna(rv_series.loc[ts]) else 0.6
                equity = cash + position.notional(mark)
                dec = risk_engine.evaluate_entry(
                    signal=sig,
                    equity=equity,
                    mark_price=mark,
                    atr_value=atr_val,
                    realized_vol_annual=rv_val,
                    position=position,
                )
                if dec.action == "open" and dec.size_quote > 0:
                    fill_px = self.fills.fill_price(mark, OrderSide.BUY)
                    qty = dec.size_quote / fill_px
                    fee = self.fills.fee(dec.size_quote)
                    cash -= dec.size_quote + fee
                    position = Position(
                        symbol="BTC/USDT",
                        qty=qty,
                        avg_price=fill_px,
                        opened_ts=ts_py,
                        stop_price=dec.stop_price,
                        take_profit_price=dec.take_profit_price,
                        time_stop_ts=dec.time_stop_ts,
                    )
                    entry_ts = ts_py
                    entry_price = fill_px

            equity_records.append((ts_py, cash + position.notional(mark)))

        equity = pd.Series(
            [e for _, e in equity_records],
            index=pd.DatetimeIndex([t for t, _ in equity_records], tz="UTC"),
            name="equity",
        )
        trades_df = pd.DataFrame(trade_records)
        if not trades_df.empty:
            trades_df["entry_ts"] = pd.to_datetime(trades_df["entry_ts"], utc=True)
            trades_df["exit_ts"] = pd.to_datetime(trades_df["exit_ts"], utc=True)

        signals_df = pd.DataFrame(
            [
                {
                    "ts": s.ts,
                    "kind": s.kind.value,
                    "score": s.score,
                    "alloc": s.target_quote_alloc,
                    "factors": ",".join(sorted(s.factors)),
                }
                for s in signals
            ]
        )

        metrics = compute_metrics(equity, trades_df)
        return BacktestResult(
            equity_curve=equity, trades=trades_df, signals=signals_df, metrics=metrics
        )


def _trade_to_dict(t: Trade) -> dict[str, object]:  # pragma: no cover - helper
    return {
        "ts": t.ts,
        "side": t.side.value,
        "qty": t.qty,
        "price": t.price,
        "fee": t.fee,
        "venue": t.venue,
        "note": t.note,
    }
