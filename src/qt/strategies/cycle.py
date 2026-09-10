"""Strategy G — Four-year cycle regime (super-long horizon).

Runs on *daily* data once every few hours, computes the 0-100 cycle
position (see ``qt.indicators.cycle``) and publishes a target BTC
allocation. It emits:

- ``buy``  when the position enters the ``accumulate`` band (≤15) or a
  Pi Cycle Bottom fires;
- ``sell`` when it enters ``distribute`` (≥85) or a Pi Cycle Top fires;
- ``watch`` otherwise, with the full component breakdown in metrics.

This layer intentionally re-emits while the condition holds — the alert
sink de-duplicates and the point is "you are still in the band".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from pydantic import BaseModel

from qt.core.config import Settings
from qt.data.market import fetch_ohlcv
from qt.data.onchain import fetch_coinmetrics_mvrv_z
from qt.indicators.cycle import cycle_position
from qt.strategies.base import EvaluationResult, Opportunity, Strategy, StrategyConfig


class CycleParams(BaseModel):
    symbol: str = "BTC/USD"
    exchange: str = "bitstamp"      # 10+ years of daily history, no auth
    history_days: int = 365 * 8
    accumulate_max: float = 15.0
    distribute_min: float = 85.0


class CycleRegime(Strategy):
    name = "cycle"
    description = "4-year cycle position (MVRV-Z, NUPL, Mayer, 200W, Pi Cycle, halving clock)."

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.params = CycleParams.model_validate(config.params or {})

    def fetch_data(self, settings: Settings) -> dict[str, Any]:
        since = datetime.now(tz=timezone.utc) - timedelta(days=self.params.history_days)
        ohlcv = fetch_ohlcv(self.params.exchange, self.params.symbol, "1d", since=since)
        onchain = fetch_coinmetrics_mvrv_z(since=since)
        return {"ohlcv": ohlcv, "onchain": onchain}

    def evaluate(self, data: dict[str, Any]) -> EvaluationResult:
        now = datetime.now(tz=timezone.utc)
        ohlcv: pd.DataFrame = data.get("ohlcv", pd.DataFrame())
        if ohlcv.empty or len(ohlcv) < 200:
            return EvaluationResult(ts=now, opportunity=None,
                                    metrics={"reason": "insufficient daily history"},
                                    notes="need ≥200 daily bars")
        oc: pd.DataFrame = data.get("onchain", pd.DataFrame())
        cp = cycle_position(
            ohlcv["close"],
            mvrv_z=oc["mvrv_z"] if not oc.empty else None,
            nupl=oc["nupl"] if not oc.empty else None,
            price_source=self.params.exchange,
        )
        pos = float(cp.position.iloc[-1])
        band = str(cp.band.iloc[-1])
        alloc = float(cp.target_alloc.iloc[-1])
        comps = {k: (None if pd.isna(v) else round(float(v), 3))
                 for k, v in cp.components.iloc[-1].items()}
        recent = cp.events.iloc[-7:]
        pi_top = bool(recent["pi_cycle_top"].any())
        pi_bot = bool(recent["pi_cycle_bottom"].any())
        metrics = {
            "cycle_position": round(pos, 1), "band": band, "target_alloc": alloc,
            "components": comps, "pi_cycle_top_7d": pi_top, "pi_cycle_bottom_7d": pi_bot,
            "price": float(ohlcv["close"].iloc[-1]),
            "latest_bar": pd.Timestamp(ohlcv.index[-1]).isoformat(),
            "bands": {k: b.as_dict() for k, b in cp.bands.items()},
            "provenance": cp.provenance,
        }
        action: str | None = None
        if pos <= self.params.accumulate_max or pi_bot:
            action = "buy"
        elif pos >= self.params.distribute_min or pi_top:
            action = "sell"
        if action is None:
            return EvaluationResult(ts=now, opportunity=None, metrics=metrics,
                                    notes=f"cycle={pos:.0f} ({band}) target={alloc:.0%}")
        opp = Opportunity(
            ts=now, action=action,  # type: ignore[arg-type]
            confidence=float(abs(pos - 50) / 50),
            reason=f"cycle position {pos:.0f}/100 → {band}; target allocation {alloc:.0%}",
            details={"symbol": self.params.symbol, **metrics},
        )
        return EvaluationResult(ts=now, opportunity=opp, metrics=metrics)


__all__ = ["CycleParams", "CycleRegime"]
