"""Strategy B — Multi-factor extreme-event capitulation buyer.

Wraps the existing ``qt.indicators.composite.compute_extreme_score`` —
the 5-factor-group + macro-veto detector used by the main backtester —
and exposes it as a *signal generator* for the multi-strategy runner.

WHEN IT FIRES
-------------
Emits a ``buy`` Opportunity when:

1. composite score ≥ ``score_min`` (default 0.6), AND
2. at least ``min_groups_firing`` of {price, vol, derivatives, on-chain,
   sentiment} groups fired (default 4), AND
3. macro filter passes (VIX/DXY).

Otherwise yields a "watch" with the current score / firing groups so
the dashboard always shows where the market stands.

PARAMETERS (in ``params:`` of the YAML)
---------------------------------------
- ``score_min``, ``min_groups_firing`` — composite threshold gate.
- ``symbol``, ``exchange``, ``timeframe`` — what to fetch.
- ``history_days`` — lookback window passed to the data adapters.

REFERENCES
----------
- Caporale, Gil-Alana, Plastun (2018) — regime-dependent BTC mean reversion.
- Gkillas & Katsiampa (2018) — extreme value theory on BTC daily returns.
- Glassnode "On-Chain Capitulation Models" (2022).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from pydantic import BaseModel

from qt.core.config import Settings
from qt.data.derivatives import (
    fetch_funding_rate_history,
    fetch_long_short_ratio,
    fetch_open_interest_history,
)
from qt.data.market import fetch_ohlcv
from qt.data.onchain import fetch_coinmetrics
from qt.data.sentiment import fetch_fear_greed
from qt.indicators.composite import compute_extreme_score
from qt.strategies.base import EvaluationResult, Opportunity, Strategy, StrategyConfig


class CapitulationParams(BaseModel):
    score_min: float = 0.60
    min_groups_firing: int = 4
    symbol: str = "BTC/USDT"
    exchange: str = "binance"
    timeframe: str = "1h"
    history_days: int = 180


class Capitulation(Strategy):
    name = "capitulation"
    description = "Multi-factor extreme-event mean-reversion buyer (5 groups + macro veto)."

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.params = CapitulationParams.model_validate(config.params or {})

    def fetch_data(self, settings: Settings) -> dict[str, Any]:
        since = datetime.now(tz=timezone.utc) - timedelta(days=self.params.history_days)
        ohlcv = fetch_ohlcv(
            self.params.exchange, self.params.symbol, self.params.timeframe,
            since=since,
        )
        funding = fetch_funding_rate_history(
            symbol=self.params.symbol.replace("/", ""), since=since,
        )
        oi = fetch_open_interest_history(symbol=self.params.symbol.replace("/", ""))
        lsr = fetch_long_short_ratio(symbol=self.params.symbol.replace("/", ""))
        fg = fetch_fear_greed(limit=0)
        mvrv = fetch_coinmetrics("mvrv", since=since)
        return {
            "ohlcv": ohlcv, "funding": funding, "oi": oi, "lsr": lsr,
            "fear_greed": fg, "mvrv": mvrv,
        }

    def evaluate(self, data: dict[str, Any]) -> EvaluationResult:
        now = datetime.now(tz=timezone.utc)
        ohlcv: pd.DataFrame = data.get("ohlcv", pd.DataFrame())
        if ohlcv.empty:
            return EvaluationResult(
                ts=now, opportunity=None,
                metrics={"reason": "no ohlcv"}, notes="waiting for data",
            )

        def _col(df: pd.DataFrame, c: str) -> pd.Series | None:
            return df[c] if isinstance(df, pd.DataFrame) and not df.empty and c in df.columns else None

        es = compute_extreme_score(
            ohlcv=ohlcv,
            funding=_col(data.get("funding", pd.DataFrame()), "funding_rate"),
            oi=_col(data.get("oi", pd.DataFrame()), "oi_usd"),
            long_short_ratio=_col(data.get("lsr", pd.DataFrame()), "long_short_ratio"),
            fear_greed=_col(data.get("fear_greed", pd.DataFrame()), "fear_greed"),
            mvrv_z=_col(data.get("mvrv", pd.DataFrame()), "mvrv"),
            cfg=None,
        )
        latest = es.score.index[-1]
        score = float(es.score.iloc[-1])
        groups_firing = int(es.group_flags.iloc[-1].sum())
        macro_ok = bool(es.macro_ok.iloc[-1])
        firing = {
            col: bool(es.group_flags[col].iloc[-1])
            for col in es.group_flags.columns
        }
        factors_now = [
            col for col in es.factor_flags.columns if bool(es.factor_flags[col].iloc[-1])
        ]

        metrics = {
            "score": round(score, 3),
            "groups_firing": groups_firing,
            "macro_ok": macro_ok,
            "group_flags": firing,
            "factors_firing": factors_now,
            "score_min": self.params.score_min,
            "min_groups_firing": self.params.min_groups_firing,
            "price": float(ohlcv["close"].iloc[-1]),
            "latest_bar": pd.Timestamp(latest).isoformat(),
        }

        triggered = (
            score >= self.params.score_min
            and groups_firing >= self.params.min_groups_firing
            and macro_ok
        )
        if not triggered:
            return EvaluationResult(
                ts=now, opportunity=None, metrics=metrics,
                notes=f"score={score:.2f} groups={groups_firing}/{self.params.min_groups_firing}",
            )

        opp = Opportunity(
            ts=now, action="buy",
            confidence=float(min(1.0, score)),
            reason=f"{groups_firing} factor groups firing at score {score:.2f}",
            details={
                "symbol": self.params.symbol,
                "score": round(score, 3),
                "groups_firing": groups_firing,
                "group_flags": firing,
                "factors_firing": factors_now,
                "price": float(ohlcv["close"].iloc[-1]),
            },
        )
        return EvaluationResult(ts=now, opportunity=opp, metrics=metrics)


__all__ = ["Capitulation", "CapitulationParams"]
