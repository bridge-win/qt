"""Strategy D — Funding-rate cash-and-carry (market-neutral basis).

When perpetual funding is sustainedly positive, a long-spot / short-perp
construction collects the funding payment with no directional exposure.

WHEN IT FIRES
-------------
- ``open``  when trailing 24h-avg funding annualizes ≥ ``enter_apr``.
- ``close`` when annualized funding ≤ ``exit_apr`` OR funding has been
  negative for ``negative_bars`` consecutive 8h prints.
- ``watch`` otherwise; metrics surface the latest annualized funding so
  the dashboard always shows the current yield.

PARAMETERS
----------
- ``enter_apr``, ``exit_apr`` — annualized funding gates (default 15% / 5%).
- ``avg_window_bars`` — trailing window for the average funding.
- ``negative_bars`` — consecutive negatives that force an exit.
- ``symbol``, ``exchange``, ``timeframe``, ``history_days``.

REFERENCES
----------
- Schrimpf, BIS WP #1106 (2023) *Crypto carry*.
- Carver (2023) *Advanced Futures Trading Strategies* — carry chapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from pydantic import BaseModel

from qt.core.config import Settings
from qt.data.derivatives import fetch_funding_rate_history
from qt.data.market import fetch_ohlcv
from qt.strategies.base import EvaluationResult, Opportunity, Strategy, StrategyConfig


class CarryParams(BaseModel):
    enter_apr: float = 0.15
    exit_apr: float = 0.05
    avg_window_bars: int = 24                      # 24h on 1h funding-aligned series
    negative_bars: int = 3
    funding_periods_per_year: float = 3 * 365.0    # 8h funding = 3/day
    symbol: str = "BTC/USDT"
    exchange: str = "binance"
    timeframe: str = "1h"
    history_days: int = 30


class BasisCarry(Strategy):
    name = "carry"
    description = "Market-neutral spot+perp funding-rate carry."

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.params = CarryParams.model_validate(config.params or {})

    def fetch_data(self, settings: Settings) -> dict[str, Any]:
        since = datetime.now(tz=timezone.utc) - timedelta(days=self.params.history_days)
        ohlcv = fetch_ohlcv(
            self.params.exchange, self.params.symbol, self.params.timeframe, since=since,
        )
        funding = fetch_funding_rate_history(
            symbol=self.params.symbol.replace("/", ""), since=since,
        )
        return {"ohlcv": ohlcv, "funding": funding}

    def evaluate(self, data: dict[str, Any]) -> EvaluationResult:
        now = datetime.now(tz=timezone.utc)
        ohlcv: pd.DataFrame = data.get("ohlcv", pd.DataFrame())
        funding_df: pd.DataFrame = data.get("funding", pd.DataFrame())
        if ohlcv.empty or funding_df.empty or "funding_rate" not in funding_df.columns:
            return EvaluationResult(
                ts=now, opportunity=None,
                metrics={"reason": "no funding data"},
                notes="waiting for derivatives data",
            )

        fund = funding_df["funding_rate"].astype("float64").sort_index()
        avg = fund.rolling(self.params.avg_window_bars).mean().dropna()
        if avg.empty:
            return EvaluationResult(
                ts=now, opportunity=None,
                metrics={"reason": "not enough funding history"},
                notes=f"need {self.params.avg_window_bars} bars",
            )
        latest_avg = float(avg.iloc[-1])
        ann = latest_avg * self.params.funding_periods_per_year
        recent_neg = int((fund.tail(self.params.negative_bars) < 0).sum())

        metrics = {
            "ann_funding": round(ann, 4),
            "enter_apr": self.params.enter_apr,
            "exit_apr": self.params.exit_apr,
            "latest_funding_8h": round(float(fund.iloc[-1]), 6),
            "negative_streak": recent_neg,
            "price": float(ohlcv["close"].iloc[-1]),
        }

        if ann >= self.params.enter_apr:
            opp = Opportunity(
                ts=now, action="open", confidence=min(1.0, ann / 0.30),
                reason=f"funding {ann:.1%} APR ≥ enter {self.params.enter_apr:.1%}",
                details={"symbol": self.params.symbol, **metrics},
            )
            return EvaluationResult(ts=now, opportunity=opp, metrics=metrics)
        if ann <= self.params.exit_apr or recent_neg >= self.params.negative_bars:
            opp = Opportunity(
                ts=now, action="close", confidence=0.7,
                reason=(
                    f"funding {ann:.1%} APR ≤ exit {self.params.exit_apr:.1%}"
                    if ann <= self.params.exit_apr else
                    f"{recent_neg} consecutive negative funding prints"
                ),
                details={"symbol": self.params.symbol, **metrics},
            )
            return EvaluationResult(ts=now, opportunity=opp, metrics=metrics)
        return EvaluationResult(
            ts=now, opportunity=None, metrics=metrics,
            notes=f"holding (ann={ann:.1%}, in [{self.params.exit_apr:.1%}, {self.params.enter_apr:.1%}])",
        )


__all__ = ["BasisCarry", "CarryParams"]
