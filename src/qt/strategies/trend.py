"""Strategy C — Weekly trend follower (Faber/Clenow inspired).

Daily evaluation; fires when the weekly close crosses the SMA(N weeks)
in either direction. The same logic underpins Mebane Faber's
*A Quantitative Approach to Tactical Asset Allocation* (2007) — the
single best-replicated systematic edge in finance.

WHEN IT FIRES
-------------
- ``open``  Opportunity on a fresh ``close > SMA(ma_weeks)`` cross-up.
- ``close`` Opportunity on a fresh ``close < SMA(ma_weeks)`` cross-down.
- otherwise ``watch`` (current state + distance to MA in the metrics).

PARAMETERS
----------
- ``ma_weeks`` — SMA lookback in weeks (default 20).
- ``vol_shock_ratio`` — if hourly RV(24h) / RV(30d) exceeds this, skip
  an entry signal (avoid breakouts in panic spikes).
- ``timeframe``, ``symbol``, ``exchange``, ``history_days`` — data shape.

REFERENCES
----------
- Faber (2007) JoWM; Moskowitz/Ooi/Pedersen (2012) JFE;
  Liu & Tsyvinski (2021) RFS; Hubrich (2017) "Know-When";
  Andreas Clenow *Trading Evolved* (2019).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel

from qt.core.config import Settings
from qt.data.market import fetch_ohlcv
from qt.strategies.base import EvaluationResult, Opportunity, Strategy, StrategyConfig


class TrendParams(BaseModel):
    ma_weeks: int = 20
    vol_shock_ratio: float = 1.8
    symbol: str = "BTC/USDT"
    exchange: str = "binance"
    timeframe: str = "1h"
    history_days: int = 365


class WeeklyTrend(Strategy):
    name = "trend"
    description = "Weekly SMA trend follower with hourly vol-shock filter."

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.params = TrendParams.model_validate(config.params or {})

    def fetch_data(self, settings: Settings) -> dict[str, Any]:
        since = datetime.now(tz=timezone.utc) - timedelta(days=self.params.history_days)
        ohlcv = fetch_ohlcv(
            self.params.exchange, self.params.symbol, self.params.timeframe,
            since=since,
        )
        return {"ohlcv": ohlcv}

    def evaluate(self, data: dict[str, Any]) -> EvaluationResult:
        now = datetime.now(tz=timezone.utc)
        ohlcv: pd.DataFrame = data.get("ohlcv", pd.DataFrame())
        if ohlcv.empty:
            return EvaluationResult(
                ts=now, opportunity=None,
                metrics={"reason": "no ohlcv"}, notes="waiting for data",
            )

        weekly = (
            ohlcv[["open", "high", "low", "close"]]
            .resample("W-MON", label="right", closed="right")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna()
        )
        if len(weekly) < self.params.ma_weeks + 2:
            return EvaluationResult(
                ts=now, opportunity=None,
                metrics={"reason": "not enough weekly history"},
                notes=f"have {len(weekly)} weeks need {self.params.ma_weeks + 2}",
            )

        wma = weekly["close"].rolling(self.params.ma_weeks).mean()
        last_close = float(weekly["close"].iloc[-1])
        last_ma = float(wma.iloc[-1])
        prev_close = float(weekly["close"].iloc[-2])
        prev_ma = float(wma.iloc[-2])

        cross_up = prev_close <= prev_ma and last_close > last_ma
        cross_down = prev_close >= prev_ma and last_close < last_ma
        in_uptrend = last_close > last_ma

        # Hourly vol-shock filter
        ret_h = ohlcv["close"].pct_change()
        rv_short = float(
            (ret_h.tail(24).std() * np.sqrt(24 * 365)) if len(ret_h) >= 24 else np.nan
        )
        rv_long = float(
            (ret_h.tail(24 * 30).std() * np.sqrt(24 * 365)) if len(ret_h) >= 24 * 30 else np.nan
        )
        vol_shock = rv_short / rv_long if rv_long and not np.isnan(rv_long) else 0.0

        metrics = {
            "weekly_close": last_close,
            "ma": last_ma,
            "distance_pct": round((last_close / last_ma - 1.0) * 100.0, 2) if last_ma else None,
            "in_uptrend": in_uptrend,
            "vol_shock": round(vol_shock, 2),
            "vol_shock_max": self.params.vol_shock_ratio,
            "cross_up": cross_up,
            "cross_down": cross_down,
        }

        if cross_up and vol_shock <= self.params.vol_shock_ratio:
            opp = Opportunity(
                ts=now, action="open", confidence=0.85,
                reason=f"Weekly close crossed above SMA({self.params.ma_weeks}w)",
                details={**metrics, "symbol": self.params.symbol},
            )
            return EvaluationResult(ts=now, opportunity=opp, metrics=metrics)
        if cross_down:
            opp = Opportunity(
                ts=now, action="close", confidence=0.90,
                reason=f"Weekly close crossed below SMA({self.params.ma_weeks}w)",
                details={**metrics, "symbol": self.params.symbol},
            )
            return EvaluationResult(ts=now, opportunity=opp, metrics=metrics)
        notes = "long bias" if in_uptrend else "cash/short bias"
        if cross_up and vol_shock > self.params.vol_shock_ratio:
            notes = f"cross-up suppressed (vol shock {vol_shock:.2f} > {self.params.vol_shock_ratio})"
        return EvaluationResult(ts=now, opportunity=None, metrics=metrics, notes=notes)


__all__ = ["TrendParams", "WeeklyTrend"]
