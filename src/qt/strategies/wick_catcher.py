"""Strategy E — Wick Catcher (earn the liquidation wick).

Maintain a ladder of deep limit-buy levels below spot (default -5 %, -8 %,
-12 %). When a liquidation cascade wicks through a rung, that rung "fills"
at panic prices; the position exits on a mean-reversion take-profit, a
ladder-bottom hard stop, or a time stop.

WHY THIS CAN WORK (see docs/RESEARCH-EARNING.md §2)
---------------------------------------------------
Crypto flash crashes are mostly *forced-seller* events: cascading margin
liquidations push price 10-30 % in minutes before reverting as the forced
selling exhausts itself. A resting deep bid buys from someone whose margin
engine — not their information — made them sell. That is a liquidity-
provision premium, and it accrues to whoever is patient enough to leave
orders in place through months of nothing.

HONEST CONSTRAINTS (same doc)
-----------------------------
- No execution guarantee in a gapping market; partial fills are normal.
- The real risk is filling into a genuine regime break, not a wick. Hence:
  small per-rung size, a hard stop below the ladder, and the macro-veto
  filter shared with the capitulation engine.
- Avoid round-number levels (order-cluster magnets) — rungs are computed
  as percentages of a moving reference, which naturally lands off-round.

As a *live signal generator* this class emits:
- ``buy`` with rung details when the latest bar's low pierces a rung
  (the runner sizes it through the RiskEngine and paper-fills it);
- ``sell`` when take-profit / stop / time-stop conditions hit while a
  ladder position is believed open (tracked via runner portfolio state);
- ``watch`` otherwise, reporting where the rungs currently sit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from pydantic import BaseModel

from qt.core.config import Settings
from qt.core.logging import get_logger
from qt.data.market import fetch_ohlcv
from qt.strategies.base import EvaluationResult, Opportunity, Strategy, StrategyConfig

log = get_logger(__name__)


class WickParams(BaseModel):
    rungs_pct: list[float] = [0.05, 0.08, 0.12]   # depth below reference
    reference_hours: int = 24                      # rolling median ref window
    take_profit_pct: float = 0.05                  # exit at +5% from fill
    hard_stop_pct: float = 0.05                    # exit at 5% below deepest rung
    max_hold_hours: int = 72                       # time stop
    per_rung_alloc: float = 0.03                   # fraction of equity per rung
    symbol: str = "BTC/USDT"
    exchange: str = "binance"
    timeframe: str = "1h"
    history_days: int = 10


class WickCatcher(Strategy):
    name = "wick"
    description = "Deep limit-buy ladder that monetizes liquidation wicks; mechanical exits."

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.params = WickParams.model_validate(config.params or {})
        # Ephemeral position memory (durable truth lives in the runner ledger;
        # this only shapes the next signal and is rebuilt on restart).
        self._entry_price: float | None = None
        self._entry_ts: datetime | None = None
        # Ladder bottom is FROZEN at entry: a stop that re-anchors to the
        # falling reference would chase the market down and never trigger.
        self._ladder_bottom: float | None = None

    # ---- data --------------------------------------------------------------

    def fetch_data(self, settings: Settings) -> dict[str, Any]:
        since = datetime.now(tz=timezone.utc) - timedelta(days=self.params.history_days)
        ohlcv = fetch_ohlcv(
            self.params.exchange, self.params.symbol, self.params.timeframe, since=since,
        )
        return {"ohlcv": ohlcv}

    # ---- evaluation ----------------------------------------------------------

    def evaluate(self, data: dict[str, Any]) -> EvaluationResult:
        ts = datetime.now(tz=timezone.utc)
        ohlcv: pd.DataFrame | None = data.get("ohlcv")
        if ohlcv is None or ohlcv.empty or len(ohlcv) < 2:
            return EvaluationResult(
                ts=ts, opportunity=None,
                metrics={"status": "no data"}, notes="waiting for OHLCV",
            )

        p = self.params
        close = ohlcv["close"]
        low = ohlcv["low"]
        last_close = float(close.iloc[-1])
        last_low = float(low.iloc[-1])

        # Reference = rolling median close over the reference window; rungs
        # hang off it so a slow drift doesn't constantly re-anchor the ladder.
        window = max(2, min(len(close), p.reference_hours))
        reference = float(close.iloc[-window:].median())
        rung_prices = [reference * (1.0 - d) for d in p.rungs_pct]

        metrics: dict[str, object] = {
            "reference": round(reference, 2),
            "last_close": round(last_close, 2),
            "last_low": round(last_low, 2),
            "rungs": [round(r, 2) for r in rung_prices],
            "in_position": self._entry_price is not None,
        }

        # ---- exit management (only when we believe a ladder fill is open) ----
        if self._entry_price is not None:
            take_profit = self._entry_price * (1.0 + p.take_profit_pct)
            deepest = self._ladder_bottom or (reference * (1.0 - p.rungs_pct[-1]))
            hard_stop = deepest * (1.0 - p.hard_stop_pct)
            expired = (
                self._entry_ts is not None
                and ts - self._entry_ts > timedelta(hours=p.max_hold_hours)
            )
            reason = None
            if last_close >= take_profit:
                reason = f"take-profit: close {last_close:,.0f} ≥ {take_profit:,.0f}"
            elif last_close <= hard_stop:
                reason = f"hard stop: close {last_close:,.0f} ≤ {hard_stop:,.0f} (regime break, not a wick)"
            elif expired:
                reason = f"time stop: held > {p.max_hold_hours}h without reversion"
            if reason:
                entry = self._entry_price
                self._entry_price = None
                self._entry_ts = None
                self._ladder_bottom = None
                return EvaluationResult(
                    ts=ts,
                    opportunity=Opportunity(
                        ts=ts, action="sell", confidence=0.9, reason=reason,
                        details={"entry_price": entry, "exit_close": last_close},
                    ),
                    metrics=metrics,
                )
            metrics["take_profit_at"] = round(take_profit, 2)
            metrics["hard_stop_at"] = round(hard_stop, 2)
            return EvaluationResult(
                ts=ts, opportunity=None, metrics=metrics,
                notes="ladder position open; managing exits",
            )

        # ---- entry: did the latest bar wick through a rung? -------------------
        pierced = [r for r in rung_prices if last_low <= r]
        if pierced:
            deepest_hit = min(pierced)
            depth_idx = rung_prices.index(deepest_hit)
            # Deeper rung hit = more panic = higher confidence in reversion
            confidence = min(1.0, 0.5 + 0.2 * depth_idx + 0.1 * len(pierced))
            self._entry_price = deepest_hit
            self._entry_ts = ts
            self._ladder_bottom = min(rung_prices)
            return EvaluationResult(
                ts=ts,
                opportunity=Opportunity(
                    ts=ts, action="buy", confidence=confidence,
                    reason=(
                        f"wick pierced {len(pierced)} rung(s); deepest {deepest_hit:,.0f} "
                        f"({p.rungs_pct[depth_idx]:.0%} below ref {reference:,.0f}) — "
                        "buying forced sellers"
                    ),
                    details={
                        "rungs_hit": [round(r, 2) for r in pierced],
                        "fill_price": round(deepest_hit, 2),
                        "bar_low": last_low,
                        "per_rung_alloc": p.per_rung_alloc * len(pierced),
                    },
                ),
                metrics=metrics,
            )

        return EvaluationResult(
            ts=ts, opportunity=None, metrics=metrics,
            notes="ladder armed; no rung touched this bar",
        )
