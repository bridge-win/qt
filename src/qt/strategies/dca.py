"""Strategy A — Smart DCA (volatility-aware periodic accumulation).

Buy a fixed quote amount on a recurring schedule (default: weekly),
but scale the *size* of each buy by a composite "stress" score built
from publicly-available retail data:

- Fear & Greed Index (alternative.me)
- MVRV-Z (optional, Glassnode community)
- 1-year drawdown from rolling high
- distance below 200d moving average

Buy multiplier = ``clip(1 + k * stress, mult_min, mult_max)``.
``stress = +1`` → "extreme fear, accumulate hard"; ``stress = -1`` →
"extreme greed, take a half-step or skip".

This strategy emits an Opportunity on every scheduled "buy bar" (e.g.
each Monday 14:00 UTC), with ``confidence = (multiplier - mult_min) /
(mult_max - mult_min)``. Days off-schedule yield a "watch" result so
the dashboard shows the current stress reading.

REFERENCES
----------
- Willy Woo, "Bitcoin Investor Tool" (2019), charts.woobull.com.
- LookIntoBitcoin / Philip Swift, MVRV-weighted DCA notebooks (2020).
- Bouri, Molnár, Azzi, Roubaud, Hagfors (2017+) on BTC volatility and
  the DCA/lump-sum trade-off.
- Glassnode Insights, "DCA Strategies for the BTC Cycle" (2022).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
from pydantic import BaseModel

from qt.core.config import Settings
from qt.core.logging import get_logger
from qt.data.market import fetch_ohlcv
from qt.data.sentiment import fetch_fear_greed
from qt.indicators.price import drawdown_from_high
from qt.strategies.base import EvaluationResult, Opportunity, Strategy, StrategyConfig

log = get_logger(__name__)


class DCAParams(BaseModel):
    base_buy_quote: float = 100.0
    buy_dow: int = 0                              # Monday
    buy_hour_utc: int = 14
    multiplier_k: float = 2.0
    mult_min: float = 0.25
    mult_max: float = 3.0
    ma_long_hours: int = 200 * 24                 # 200d MA on hourly bars
    dd_window_hours: int = 365 * 24               # 1y drawdown window
    timeframe: str = "1h"
    history_days: int = 250
    symbol: str = "BTC/USDT"
    exchange: str = "binance"


class SmartDCA(Strategy):
    name = "dca"
    description = "Volatility-aware weekly DCA (fear-scaled buy amount)."

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.params = DCAParams.model_validate(config.params or {})

    def fetch_data(self, settings: Settings) -> dict[str, Any]:
        from datetime import timedelta
        since = datetime.now(tz=timezone.utc) - timedelta(days=self.params.history_days)
        ohlcv = fetch_ohlcv(
            self.params.exchange, self.params.symbol, self.params.timeframe,
            since=since,
        )
        fg = fetch_fear_greed(limit=0)
        return {"ohlcv": ohlcv, "fear_greed": fg}

    def evaluate(self, data: dict[str, Any]) -> EvaluationResult:
        now = datetime.now(tz=timezone.utc)
        ohlcv: pd.DataFrame = data.get("ohlcv", pd.DataFrame())
        fg: pd.DataFrame = data.get("fear_greed", pd.DataFrame())
        if ohlcv.empty:
            return EvaluationResult(
                ts=now, opportunity=None,
                metrics={"reason": "no ohlcv"}, notes="waiting for data",
            )

        close = ohlcv["close"].astype("float64")
        stress, components = _stress_score(close, fg, self.params)
        latest_stress = float(stress.iloc[-1])
        multiplier = max(
            self.params.mult_min,
            min(self.params.mult_max, 1.0 + self.params.multiplier_k * latest_stress),
        )

        is_buy_now = (
            now.weekday() == self.params.buy_dow
            and now.hour == self.params.buy_hour_utc
        )
        metrics = {
            "stress": round(latest_stress, 3),
            "multiplier": round(multiplier, 3),
            "base_buy_quote": self.params.base_buy_quote,
            "components": {k: round(float(v), 3) for k, v in components.items()},
            "price": float(close.iloc[-1]),
            "is_buy_window": is_buy_now,
        }

        if not is_buy_now:
            return EvaluationResult(
                ts=now, opportunity=None, metrics=metrics,
                notes=f"next buy: dow={self.params.buy_dow} hour={self.params.buy_hour_utc}",
            )

        confidence = (multiplier - self.params.mult_min) / max(
            1e-9, self.params.mult_max - self.params.mult_min,
        )
        amount = round(self.params.base_buy_quote * multiplier, 2)
        opp = Opportunity(
            ts=now,
            action="buy",
            confidence=float(confidence),
            reason=f"weekly DCA buy ${amount} ({multiplier:.2f}x base)",
            details={
                "amount_quote": amount,
                "multiplier": round(multiplier, 3),
                "stress": round(latest_stress, 3),
                "price": float(close.iloc[-1]),
                "symbol": self.params.symbol,
                "components": metrics["components"],
            },
        )
        return EvaluationResult(ts=now, opportunity=opp, metrics=metrics)


def _stress_score(
    close: pd.Series, fg: pd.DataFrame, params: DCAParams,
) -> tuple[pd.Series, dict[str, float]]:
    """Composite stress in [-1, +1] = mean of available components.

    Each component is normalized so +1 = "maximum buy signal"
    (extreme fear / deepest drawdown / price below MA) and -1 =
    "maximum skip" (greed / ATH).
    """

    parts: list[pd.Series] = []
    components: dict[str, float] = {}

    if not fg.empty and "fear_greed" in fg.columns:
        fg_series = fg["fear_greed"].astype("float64").reindex(close.index).ffill().shift(1)
        s_fg = ((50.0 - fg_series) / 40.0).clip(-1, 1)
        parts.append(s_fg)
        components["fear_greed"] = float(s_fg.iloc[-1]) if not s_fg.empty else 0.0

    dd = drawdown_from_high(close, window=params.dd_window_hours)
    s_dd = (-dd / 0.60).clip(-1, 1)
    parts.append(s_dd)
    components["drawdown"] = float(s_dd.iloc[-1])

    ma_long = close.rolling(params.ma_long_hours).mean()
    rel = (close / ma_long) - 1.0
    s_ma = ((-rel) / 0.40).clip(-1, 1)
    parts.append(s_ma)
    components["ma_distance"] = float(s_ma.iloc[-1]) if pd.notna(s_ma.iloc[-1]) else 0.0

    stack = pd.concat(parts, axis=1)
    return stack.mean(axis=1).fillna(0.0), components


__all__ = ["DCAParams", "SmartDCA"]
