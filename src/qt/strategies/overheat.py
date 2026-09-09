"""Strategy F — Overheat / distribution detector (weekly-cadence *sell* side).

The mirror of ``capitulation``: it never shorts, it tells you when to
*stop adding* and to trim. Emits a ``sell`` opportunity when enough
independent groups say the market is over-extended:

- price:       RSI(14) > 80 | BB-Z ≥ +2.5 | (close-SMA20)/ATR14 ≥ 3 | 30d run-up ≥ 40 %
- derivatives: funding Z ≥ +2 | funding ≥ +0.05 %/8h for 3 prints | OI +15 %/24h | short-liq spike
- on-chain:    MVRV-Z > 7 | NUPL > 0.75 | Pi Cycle Top event (7-day window)
- sentiment:   Fear & Greed ≥ 80 for 3 days

Trigger: score ≥ ``score_min`` and ≥ ``min_groups_firing`` groups. The
on-chain group turns this from a "local top" detector into a
"cycle top" detector when it fires.

REFERENCES
----------
- Glassnode "Euphoria" NUPL bands; Awe & Mahmudov MVRV-Z (2018)
- Philip Swift, Pi Cycle Top (2021)
- Practitioner consensus that tops are *processes* (low vol, funding-led)
  whereas bottoms are *events* (high vol, liquidation-led) — hence no
  volatility group on this side.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from pydantic import BaseModel

from qt.core.config import Settings
from qt.data.coinglass import fetch_aggregated_liquidations
from qt.data.derivatives import fetch_funding_rate_history, fetch_open_interest_history
from qt.data.market import fetch_ohlcv
from qt.data.onchain import fetch_coinmetrics_mvrv_z
from qt.data.sentiment import fetch_fear_greed
from qt.indicators.composite import compute_overheat_score
from qt.strategies.base import EvaluationResult, Opportunity, Strategy, StrategyConfig


class OverheatParams(BaseModel):
    score_min: float = 0.60
    min_groups_firing: int = 3
    symbol: str = "BTC/USDT"
    exchange: str = "binance"
    timeframe: str = "1h"
    history_days: int = 400   # ≥ 350 daily closes for Pi Cycle Top


class Overheat(Strategy):
    name = "overheat"
    description = "Multi-group overheat/distribution detector (sell-side mirror of capitulation)."

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.params = OverheatParams.model_validate(config.params or {})

    def fetch_data(self, settings: Settings) -> dict[str, Any]:
        since = datetime.now(tz=timezone.utc) - timedelta(days=self.params.history_days)
        market = self.params.symbol.replace("/", "")
        ohlcv = fetch_ohlcv(self.params.exchange, self.params.symbol, self.params.timeframe, since=since)
        funding = fetch_funding_rate_history(symbol=market, since=since)
        oi = fetch_open_interest_history(symbol=market)
        fg = fetch_fear_greed(limit=0)
        onchain = fetch_coinmetrics_mvrv_z(since=since)
        liq = fetch_aggregated_liquidations(settings.coinglass_api_key, since=since)
        return {"ohlcv": ohlcv, "funding": funding, "oi": oi, "fear_greed": fg,
                "onchain": onchain, "liq": liq}

    def evaluate(self, data: dict[str, Any]) -> EvaluationResult:
        now = datetime.now(tz=timezone.utc)
        ohlcv: pd.DataFrame = data.get("ohlcv", pd.DataFrame())
        if ohlcv.empty:
            return EvaluationResult(ts=now, opportunity=None,
                                    metrics={"reason": "no ohlcv"}, notes="waiting for data")

        def _col(df: pd.DataFrame, c: str) -> pd.Series | None:
            return df[c] if isinstance(df, pd.DataFrame) and not df.empty and c in df.columns else None

        es = compute_overheat_score(
            ohlcv=ohlcv,
            funding=_col(data.get("funding", pd.DataFrame()), "funding_rate"),
            oi=_col(data.get("oi", pd.DataFrame()), "oi_usd"),
            mvrv_z=_col(data.get("onchain", pd.DataFrame()), "mvrv_z"),
            nupl=_col(data.get("onchain", pd.DataFrame()), "nupl"),
            fear_greed=_col(data.get("fear_greed", pd.DataFrame()), "fear_greed"),
            short_liq_usd=_col(data.get("liq", pd.DataFrame()), "short_liq_usd"),
        )
        score = float(es.score.iloc[-1])
        groups_firing = int(es.group_flags.iloc[-1].sum())
        firing = {c: bool(es.group_flags[c].iloc[-1]) for c in es.group_flags.columns}
        factors_now = [c for c in es.factor_flags.columns if bool(es.factor_flags[c].iloc[-1])]
        metrics = {
            "score": round(score, 3), "groups_firing": groups_firing, "group_flags": firing,
            "factors_firing": factors_now, "score_min": self.params.score_min,
            "min_groups_firing": self.params.min_groups_firing,
            "price": float(ohlcv["close"].iloc[-1]),
            "latest_bar": pd.Timestamp(es.score.index[-1]).isoformat(),
            "cycle_top_flag": bool(firing.get("onchain", False)),
        }
        if not (score >= self.params.score_min and groups_firing >= self.params.min_groups_firing):
            return EvaluationResult(ts=now, opportunity=None, metrics=metrics,
                                    notes=f"overheat={score:.2f} groups={groups_firing}")
        severity_note = "CYCLE-TOP class (on-chain firing)" if firing.get("onchain") else "local overheat"
        opp = Opportunity(
            ts=now, action="sell", confidence=float(min(1.0, score)),
            reason=f"{groups_firing} overheat groups firing at {score:.2f} — {severity_note}",
            details={"symbol": self.params.symbol, "score": round(score, 3),
                     "groups_firing": groups_firing, "group_flags": firing,
                     "factors_firing": factors_now, "price": metrics["price"],
                     "suggested_action": "trim 20-30% / stop DCA" if not firing.get("onchain")
                     else "distribute per cycle target_alloc"},
        )
        return EvaluationResult(ts=now, opportunity=opp, metrics=metrics)


__all__ = ["Overheat", "OverheatParams"]
