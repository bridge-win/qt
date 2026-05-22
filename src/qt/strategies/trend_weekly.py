"""Solution C — Weekly trend following (Faber / Clenow style).

WHY THIS STRATEGY EXISTS
------------------------
The single most replicated systematic edge in finance — time-series
momentum / trend-following — works on crypto as well as on commodities
and stocks. Key references:

- Moskowitz, Ooi, Pedersen (2012), *Time-Series Momentum* (JFE): 12-month
  past return positive ⇒ next-month return positive across 58 markets,
  decades of data.
- Liu, Tsyvinski (2021), *Risks and Returns of Cryptocurrency* (Review
  of Financial Studies): momentum factor (1-week to 8-week) significant
  in BTC and the broader crypto cross-section.
- Faber (2007), *A Quantitative Approach to Tactical Asset Allocation*:
  10-month SMA filter on monthly bars roughly **halves max drawdown**
  while keeping CAGR ~unchanged for SPX, EAFE, US bonds. The crypto
  analog is the 20-week SMA on weekly bars.
- Hubrich (2017), "Know-When": the 200d MA reduces BTC MaxDD from −85%
  to ~−30% with negligible CAGR cost.
- Andreas Clenow (*Trading Evolved*, *Following the Trend*): systematic
  trend on weekly bars with vol-targeted sizing is "the upper bound on
  what a single individual can reliably do".

Design:
1. Resample to weekly bars (Sunday/Monday UTC close depending on venue).
2. Long when ``close > SMA(n_weeks)`` (default 20 weeks ≈ 140d).
3. Position size = inverse realized volatility, targeting
   ``target_vol_annual`` (default 15%).
4. Exit when close crosses below SMA OR ``trailing_atr_k × ATR(14w)``
   trailing stop triggers.
5. Optional "vol-shock filter": if ``rv_short / rv_long > vol_shock_ratio``,
   force flat — avoids buying breakouts in the middle of a panic spike.

PARAMETERS
----------
- ``ma_weeks``:           SMA lookback in weeks (default 20).
- ``trailing_atr_k``:     ATR multiple for trailing stop (default 3.0).
- ``atr_weeks``:          ATR period in weeks (default 14).
- ``target_vol_annual``:  vol-target (default 0.15 = 15% annualized).
- ``vol_shock_ratio``:    short/long realized-vol cap (default 1.8).
- ``max_weight``:         cap on long allocation (default 1.0).

KNOWN FAILURE MODES
-------------------
- **Choppy markets**: when price oscillates around the SMA, you get
  a string of small whipsaw losses. Default ``ma_weeks=20`` is biased
  toward fewer false signals than 10w; ``trailing_atr_k=3.0`` lets the
  position breathe.
- **Lagged exit at tops**: a SMA-based exit will give back ~15–20%
  from the absolute peak. That's intrinsic to trend-following; if it's
  unacceptable, combine with Solution B's MVRV-Z mean-revert exit.
- **Data alignment**: weekly bars start on Monday 00:00 UTC by default;
  major venues may have different "week start" conventions.

REFERENCES
----------
- Faber (2007), JoWM.
- Moskowitz, Ooi, Pedersen (2012), JFE.
- Liu, Tsyvinski (2021), RFS.
- Andreas Clenow (2017, 2019), *Following the Trend*, *Trading Evolved*.
- Robert Carver (2015, 2023), *Systematic Trading*, *Advanced Futures
  Trading Strategies*.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qt.strategies.base import StrategyResult


@dataclass
class WeeklyTrendConfig:
    ma_weeks: int = 20
    trailing_atr_k: float = 3.0
    atr_weeks: int = 14
    target_vol_annual: float = 0.15
    vol_shock_ratio: float = 1.8
    max_weight: float = 1.0
    fee_bps: float = 7.5
    slippage_bps: float = 5.0
    initial_cash: float = 10_000.0
    bars_per_week: int = 24 * 7
    weeks_per_year: int = 52


class WeeklyTrend:
    """Weekly-bar trend follower with vol-targeted sizing. See module docstring."""

    def __init__(self, cfg: WeeklyTrendConfig | None = None) -> None:
        self.cfg = cfg or WeeklyTrendConfig()

    def run(self, ohlcv: pd.DataFrame) -> StrategyResult:
        cfg = self.cfg

        # Resample hourly OHLCV to weekly. The DataFrame is expected to be
        # indexed by UTC timestamps. We use 'W-MON' so that week-end falls
        # on Sunday 23:59 (Monday-start convention).
        weekly = (
            ohlcv[["open", "high", "low", "close"]]
            .resample("W-MON", label="right", closed="right")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna()
        )
        if weekly.empty:
            empty = pd.Series(dtype="float64")
            return StrategyResult(
                equity=empty, target_weight=empty,
                short_weight=empty, trades=pd.DataFrame(),
                diagnostics=pd.DataFrame(),
            )

        wclose = weekly["close"]
        wma = wclose.rolling(cfg.ma_weeks).mean()

        # Weekly ATR (Wilder smoothing approximation via EWMA).
        prev = wclose.shift(1)
        tr = pd.concat(
            [
                (weekly["high"] - weekly["low"]).abs(),
                (weekly["high"] - prev).abs(),
                (weekly["low"] - prev).abs(),
            ], axis=1,
        ).max(axis=1)
        atrw = tr.ewm(alpha=1 / cfg.atr_weeks, adjust=False).mean()

        # Vol-shock filter: hourly realized vol short-vs-long.
        ret_h = ohlcv["close"].pct_change()
        rv_short = ret_h.rolling(24).std() * np.sqrt(24 * 365)
        rv_long = ret_h.rolling(24 * 30).std() * np.sqrt(24 * 365)
        vol_shock = (rv_short / rv_long).reindex(wclose.index, method="ffill")

        long_signal = (wclose > wma) & (vol_shock <= cfg.vol_shock_ratio)

        cash = cfg.initial_cash
        qty = 0.0
        peak = 0.0
        weekly_equity: list[float] = []
        weekly_tw: list[float] = []
        trades: list[dict] = []

        # Inverse-vol sizing on weekly bars
        wret = wclose.pct_change()
        wrv = wret.rolling(cfg.ma_weeks).std() * np.sqrt(cfg.weeks_per_year)
        size_w = (cfg.target_vol_annual / wrv).clip(0.0, cfg.max_weight)

        cost = (cfg.fee_bps + cfg.slippage_bps) / 10_000.0
        for ts, px in wclose.items():
            px_f = float(px)
            equity = cash + qty * px_f
            if qty > 0:
                peak = max(peak, px_f)
                trail_stop = peak - cfg.trailing_atr_k * float(atrw.loc[ts])
                exit_signal = (
                    not bool(long_signal.loc[ts])
                    or px_f <= trail_stop
                )
                if exit_signal:
                    proceeds = qty * px_f
                    fee = proceeds * cost
                    cash += proceeds - fee
                    trades.append(
                        {"ts": ts, "side": "sell", "qty": -qty, "price": px_f,
                         "fee": fee, "leg": "spot",
                         "note": "ma_cross_below" if not bool(long_signal.loc[ts])
                                 else "trail_stop"}
                    )
                    qty = 0.0
                    peak = 0.0
            elif bool(long_signal.loc[ts]) and pd.notna(size_w.loc[ts]):
                w = float(size_w.loc[ts])
                if w > 0:
                    notional = w * equity
                    notional = min(notional, cash)
                    fee = notional * cost
                    bought = (notional - fee) / px_f
                    qty = bought
                    cash -= notional
                    peak = px_f
                    trades.append(
                        {"ts": ts, "side": "buy", "qty": bought, "price": px_f,
                         "fee": fee, "leg": "spot",
                         "note": f"entry_w={w:.2f}"}
                    )
            eq_now = cash + qty * px_f
            weekly_equity.append(eq_now)
            weekly_tw.append((qty * px_f) / eq_now if eq_now > 0 else 0.0)

        # Forward-fill equity & target weight onto the original hourly index
        weekly_eq_series = pd.Series(weekly_equity, index=wclose.index)
        weekly_tw_series = pd.Series(weekly_tw, index=wclose.index)
        hourly_eq = weekly_eq_series.reindex(ohlcv.index, method="ffill")
        # Initial cash for the bars before the first weekly bar
        hourly_eq = hourly_eq.fillna(cfg.initial_cash).rename("equity")
        hourly_tw = weekly_tw_series.reindex(ohlcv.index, method="ffill").fillna(0.0)

        diag = pd.DataFrame(
            {"wclose": wclose, "wma": wma, "atrw": atrw, "size_w": size_w,
             "long_signal": long_signal}
        )
        return StrategyResult(
            equity=hourly_eq,
            target_weight=hourly_tw,
            short_weight=pd.Series(0.0, index=ohlcv.index),
            trades=pd.DataFrame(trades),
            diagnostics=diag,
        )


__all__ = ["WeeklyTrend", "WeeklyTrendConfig"]
