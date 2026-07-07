"""Strategy E — Wick catcher deep-limit ladder.

This live signal generator watches the latest candle for lower-wick pierces of
predefined ladder rungs below the prior close. It does not place orders itself;
it emits a paper-first buy opportunity that the operator can route through the
existing execution path after paper validation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from pydantic import BaseModel

from qt.core.config import Settings
from qt.data.market import fetch_ohlcv
from qt.strategies.base import EvaluationResult, Opportunity, Strategy, StrategyConfig


class WickCatcherParams(BaseModel):
    ladder_drawdowns: list[float] = [0.05, 0.08, 0.12]
    take_profit_pct: float = 0.03
    rung_quote: float = 100.0
    symbol: str = "BTC/USDT"
    exchange: str = "binance"
    timeframe: str = "1m"
    history_days: int = 2


class WickCatcher(Strategy):
    name = "wick"
    description = "Deep-limit ladder for liquidation-wick mean reversion."

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.params = WickCatcherParams.model_validate(config.params or {})

    def fetch_data(self, settings: Settings) -> dict[str, Any]:
        since = datetime.now(tz=timezone.utc) - timedelta(days=self.params.history_days)
        ohlcv = fetch_ohlcv(
            self.params.exchange,
            self.params.symbol,
            self.params.timeframe,
            since=since,
        )
        return {"ohlcv": ohlcv}

    def evaluate(self, data: dict[str, Any]) -> EvaluationResult:
        now = datetime.now(tz=timezone.utc)
        ohlcv: pd.DataFrame = data.get("ohlcv", pd.DataFrame())
        if ohlcv.empty or len(ohlcv) < 2:
            return EvaluationResult(
                ts=now,
                opportunity=None,
                metrics={"reason": "need at least two candles"},
                notes="waiting for wick data",
            )

        latest = ohlcv.iloc[-1]
        prior_close = float(ohlcv["close"].iloc[-2])
        low = float(latest["low"])
        close = float(latest["close"])
        rungs = sorted(float(x) for x in self.params.ladder_drawdowns if float(x) > 0)
        rung_prices = {drawdown: prior_close * (1.0 - drawdown) for drawdown in rungs}
        filled = [drawdown for drawdown, price in rung_prices.items() if low <= price]
        nearest_price = next(iter(rung_prices.values()), prior_close)
        metrics = {
            "spot_price": round(close, 8),
            "prior_close": round(prior_close, 8),
            "latest_low": round(low, 8),
            "nearest_rung_price": round(nearest_price, 8),
            "ladder_drawdowns": rungs,
            "rung_prices": {str(k): round(v, 8) for k, v in rung_prices.items()},
            "filled_rungs": filled,
            "take_profit_pct": self.params.take_profit_pct,
            "rung_quote": self.params.rung_quote,
        }
        if not filled:
            return EvaluationResult(
                ts=now,
                opportunity=None,
                metrics=metrics,
                notes=f"nearest rung {nearest_price:.2f}",
            )

        fill_prices = [rung_prices[drawdown] for drawdown in filled]
        confidence = min(0.95, 0.45 + 0.15 * len(filled))
        opp = Opportunity(
            ts=now,
            action="buy",
            confidence=confidence,
            reason=f"{len(filled)} wick ladder rung(s) pierced",
            details={
                "symbol": self.params.symbol,
                "exchange": self.params.exchange,
                "timeframe": self.params.timeframe,
                "filled_rungs": filled,
                "fill_prices": [round(price, 8) for price in fill_prices],
                "take_profit_prices": [
                    round(price * (1.0 + self.params.take_profit_pct), 8)
                    for price in fill_prices
                ],
                "rung_quote": self.params.rung_quote,
                "latest_low": low,
                "prior_close": prior_close,
            },
        )
        return EvaluationResult(ts=now, opportunity=opp, metrics=metrics)


__all__ = ["WickCatcher", "WickCatcherParams"]
