"""The `intel` pseudo-strategy: plugs the scanner sweep into the existing
multi-strategy runner so it gets a thread, a heartbeat, a dashboard sub-route,
and alerting for free.

It only ever emits ``action="watch"`` opportunities — the runner executes
nothing for "watch"; it just alerts and records. Execution stays with the
real strategies / the human, per docs/RESEARCH-EARNING.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel

from qt.core.config import Settings
from qt.core.logging import get_logger
from qt.intel.ranker import rank_findings, write_findings
from qt.intel.scanners import (
    BasisScanner,
    DepegScanner,
    FundingScanner,
    SpreadScanner,
    WickScanner,
)
from qt.intel.types import IntelFinding
from qt.strategies.base import EvaluationResult, Opportunity, Strategy, StrategyConfig

log = get_logger(__name__)


class IntelParams(BaseModel):
    venues: list[str] = ["binance", "okx", "bybit"]
    symbol: str = "BTC/USDT"
    stables: list[str] = ["USDT", "USDC", "DAI"]
    min_carry_apr: float = 0.10
    min_diff_apr: float = 0.05
    min_spread_net_bps: float = 10.0
    min_depeg_bps: float = 30.0
    alert_min_net_bps: float = 50.0     # alert only when top finding ≥ this
    ohlcv_history_hours: int = 96
    exchange: str = "binance"
    timeframe: str = "1h"


class IntelDiscovery(Strategy):
    """Continuous opportunity scan → rank net-of-fees → persist + alert."""

    name = "intel"
    description = "Intelligence Discovery: funding/spread/basis/depeg/wick scanners, ranked net of fees."

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.params = IntelParams(**config.params)
        p = self.params
        self.funding = FundingScanner(
            symbol=p.symbol, min_carry_apr=p.min_carry_apr, min_diff_apr=p.min_diff_apr,
        )
        self.spread = SpreadScanner(symbol=p.symbol, min_net_bps=p.min_spread_net_bps)
        self.basis = BasisScanner(symbol=p.symbol)
        self.depeg = DepegScanner(min_deviation_bps=p.min_depeg_bps)
        self.wick = WickScanner()

    # ---- data ------------------------------------------------------------

    def fetch_data(self, settings: Settings) -> dict[str, Any]:
        """Best-effort live pulls; every piece is optional."""
        p = self.params
        data: dict[str, Any] = {}
        data["funding_rates"] = self.funding.fetch(p.venues)
        data["tickers"] = self.spread.fetch(p.venues)
        data["stable_prices"] = self._fetch_stables(p)
        data["ohlcv"] = self._fetch_ohlcv(settings, p)
        return data

    def _fetch_stables(self, p: IntelParams) -> dict[str, float]:
        out: dict[str, float] = {}
        try:
            import ccxt

            client = getattr(ccxt, p.exchange)({"enableRateLimit": True})
            for coin in p.stables:
                if coin == "USDT":
                    continue  # needs a USDT-quoted pair below anyway
                try:
                    t = client.fetch_ticker(f"{coin}/USDT")
                    if t.get("last"):
                        out[coin] = float(t["last"])
                except Exception:
                    continue
        except Exception as exc:
            log.warning("stable_fetch_failed", error=str(exc))
        return out

    def _fetch_ohlcv(self, settings: Settings, p: IntelParams) -> pd.DataFrame | None:
        try:
            from qt.data.market import fetch_ohlcv

            since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=p.ohlcv_history_hours)
            df = fetch_ohlcv(p.exchange, p.symbol, p.timeframe, since=since.to_pydatetime())
            return df if not df.empty else None
        except Exception as exc:
            log.warning("intel_ohlcv_failed", error=str(exc))
            return None

    # ---- evaluation --------------------------------------------------------

    def evaluate(self, data: dict[str, Any]) -> EvaluationResult:
        ts = datetime.now(tz=timezone.utc)
        findings: list[IntelFinding] = []
        findings += self.funding.evaluate(data.get("funding_rates") or {})
        findings += self.spread.evaluate(data.get("tickers") or {})
        findings += self.depeg.evaluate(data.get("stable_prices") or {})
        findings += self.wick.evaluate(data.get("ohlcv"))
        spot = data.get("spot_price")
        fut = data.get("future_price")
        dte = data.get("days_to_expiry")
        findings += self.basis.evaluate(spot, fut, dte)

        ranked = rank_findings(findings)
        write_findings(ranked, runtime_dir=Path("data/runtime"))

        metrics: dict[str, object] = {
            "findings": len(ranked),
            "best_kind": ranked[0].kind if ranked else None,
            "best_net_bps": round(ranked[0].net_edge_bps, 1) if ranked else 0.0,
            "scanners_with_data": sum(
                1 for k in ("funding_rates", "tickers", "stable_prices", "ohlcv")
                if data.get(k)
            ),
        }

        opportunity = None
        if ranked and ranked[0].net_edge_bps >= self.params.alert_min_net_bps:
            best = ranked[0]
            opportunity = Opportunity(
                ts=ts, action="watch",
                confidence=best.confidence,
                reason=f"[{best.kind}] {best.reason}",
                details=best.as_dict(),
            )

        return EvaluationResult(
            ts=ts, opportunity=opportunity, metrics=metrics,
            notes="detection only — no automatic execution",
        )
