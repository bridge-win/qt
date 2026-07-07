"""Opportunity scanners. Each has a pure ``evaluate(data, ...)`` (unit-testable,
no network) and a ``fetch(settings)`` that pulls live data via ccxt and degrades
gracefully (returns ``{}``) when a venue is unreachable.

Fee model: taker fees per leg (default 10 bps/leg retail spot taker, 5 bps/leg
perp taker) + a slippage buffer. Every finding's ``net_edge_bps`` already has
all legs subtracted — the ranker never sees gross numbers alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from qt.core.logging import get_logger
from qt.indicators.events import flash_crash, wick_cluster
from qt.indicators.price import wick_ratio
from qt.intel.types import IntelFinding

log = get_logger(__name__)

FUNDING_PERIODS_PER_YEAR = 3 * 365  # 8h funding


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _ccxt_client(venue: str) -> Any:
    import ccxt

    return getattr(ccxt, venue)({"enableRateLimit": True})


@dataclass
class FundingScanner:
    """Single-venue carry APR + cross-venue funding differentials.

    Carry: annualized funding collected by spot-long + perp-short on one venue.
    Differential: long perp where funding is lowest, short perp where highest —
    collects the spread while price exposure nets out.
    """

    symbol: str = "BTC/USDT"
    min_carry_apr: float = 0.10          # surface single-venue carry ≥ 10% APR
    min_diff_apr: float = 0.05           # surface cross-venue spread ≥ 5% APR
    spot_taker_bps: float = 10.0
    perp_taker_bps: float = 5.0
    slippage_bps: float = 3.0
    holding_days: float = 30.0           # amortize entry/exit fees over this

    def fetch(self, venues: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for v in venues:
            try:
                client = _ccxt_client(v)
                fr = client.fetch_funding_rate(self.symbol.replace("/", "/") + ":USDT")
                rate = fr.get("fundingRate")
                if rate is not None:
                    out[v] = float(rate)
            except Exception as exc:
                log.warning("funding_fetch_failed", venue=v, error=str(exc))
        return out

    def evaluate(self, rates_8h: dict[str, float]) -> list[IntelFinding]:
        findings: list[IntelFinding] = []
        ts = _now()
        # Entry+exit: spot (2 legs) + perp (2 legs) + slippage, amortized per year
        carry_fee_bps_annual = (
            (2 * self.spot_taker_bps + 2 * self.perp_taker_bps + 2 * self.slippage_bps)
            * (365.0 / max(self.holding_days, 1.0))
        )
        for venue, rate in rates_8h.items():
            apr = rate * FUNDING_PERIODS_PER_YEAR
            if apr >= self.min_carry_apr:
                gross_bps = apr * 10_000
                net_bps = gross_bps - carry_fee_bps_annual
                if net_bps > 0:
                    findings.append(IntelFinding(
                        ts=ts, kind="funding_carry", symbol=self.symbol,
                        venues=(venue,),
                        gross_edge_bps=gross_bps, fees_bps=carry_fee_bps_annual,
                        net_edge_bps=net_bps,
                        confidence=min(1.0, apr / 0.30),
                        reason=(
                            f"{venue} funding is {rate * 100:.4f}%/8h "
                            f"({apr:.1%} APR) — spot-long + perp-short collects it"
                        ),
                        action_hint="carry strategy handles this automatically",
                        details={"funding_8h": rate, "apr": apr},
                    ))
        # Cross-venue differential
        if len(rates_8h) >= 2:
            hi_v = max(rates_8h, key=lambda v: rates_8h[v])
            lo_v = min(rates_8h, key=lambda v: rates_8h[v])
            diff_apr = (rates_8h[hi_v] - rates_8h[lo_v]) * FUNDING_PERIODS_PER_YEAR
            if hi_v != lo_v and diff_apr >= self.min_diff_apr:
                diff_fee_bps_annual = (
                    (4 * self.perp_taker_bps + 2 * self.slippage_bps)
                    * (365.0 / max(self.holding_days, 1.0))
                )
                gross_bps = diff_apr * 10_000
                net_bps = gross_bps - diff_fee_bps_annual
                if net_bps > 0:
                    findings.append(IntelFinding(
                        ts=ts, kind="funding_diff", symbol=self.symbol,
                        venues=(hi_v, lo_v),
                        gross_edge_bps=gross_bps, fees_bps=diff_fee_bps_annual,
                        net_edge_bps=net_bps,
                        confidence=min(1.0, diff_apr / 0.20),
                        reason=(
                            f"funding spread: short perp on {hi_v} "
                            f"({rates_8h[hi_v] * 100:.4f}%/8h) vs long perp on {lo_v} "
                            f"({rates_8h[lo_v] * 100:.4f}%/8h) = {diff_apr:.1%} APR"
                        ),
                        action_hint="cross-venue perp pair; price exposure nets out",
                        details={"rates_8h": dict(rates_8h)},
                    ))
        return findings


@dataclass
class SpreadScanner:
    """Cross-venue spot spread, net of both taker fees + slippage buffer.

    Evidence says these windows are professional territory at speed — we only
    surface FAT spreads (panic-sized) that persist long enough for a human.
    """

    symbol: str = "BTC/USDT"
    taker_bps: float = 10.0
    slippage_bps: float = 5.0
    min_net_bps: float = 10.0    # only surface if ≥ 10 bps NET

    def fetch(self, venues: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for v in venues:
            try:
                t = _ccxt_client(v).fetch_ticker(self.symbol)
                bid, ask = t.get("bid"), t.get("ask")
                if bid and ask:
                    out[v] = {"bid": float(bid), "ask": float(ask)}
            except Exception as exc:
                log.warning("ticker_fetch_failed", venue=v, error=str(exc))
        return out

    def evaluate(self, books: dict[str, dict[str, float]]) -> list[IntelFinding]:
        if len(books) < 2:
            return []
        ts = _now()
        findings: list[IntelFinding] = []
        venues = list(books)
        for buy_v in venues:               # buy at ask here
            for sell_v in venues:          # sell at bid there
                if buy_v == sell_v:
                    continue
                ask = books[buy_v]["ask"]
                bid = books[sell_v]["bid"]
                if ask <= 0:
                    continue
                mid = (ask + bid) / 2
                gross_bps = (bid - ask) / mid * 10_000
                fees = 2 * self.taker_bps + 2 * self.slippage_bps
                net_bps = gross_bps - fees
                if net_bps >= self.min_net_bps:
                    findings.append(IntelFinding(
                        ts=ts, kind="spread", symbol=self.symbol,
                        venues=(buy_v, sell_v),
                        gross_edge_bps=gross_bps, fees_bps=fees, net_edge_bps=net_bps,
                        confidence=0.4,  # spreads decay fast; low confidence by design
                        reason=(
                            f"buy {self.symbol} on {buy_v} @ {ask:,.2f}, "
                            f"sell on {sell_v} @ {bid:,.2f} = {net_bps:.0f} bps net"
                        ),
                        action_hint="needs inventory on BOTH venues; do not chase with transfers",
                        details={"ask": ask, "bid": bid},
                    ))
        return findings


@dataclass
class BasisScanner:
    """Spot vs dated-futures annualized basis (cash-and-carry windows)."""

    symbol: str = "BTC/USDT"
    min_basis_apr: float = 0.08
    taker_bps: float = 10.0
    slippage_bps: float = 3.0

    def evaluate(
        self, spot: float | None, future: float | None, days_to_expiry: float | None,
    ) -> list[IntelFinding]:
        if not spot or not future or not days_to_expiry or days_to_expiry <= 0:
            return []
        ts = _now()
        basis = (future - spot) / spot
        apr = basis * (365.0 / days_to_expiry)
        if apr < self.min_basis_apr:
            return []
        fees = 4 * self.taker_bps + 2 * self.slippage_bps  # in+out both legs
        gross_bps = basis * 10_000
        net_bps = gross_bps - fees
        if net_bps <= 0:
            return []
        return [IntelFinding(
            ts=ts, kind="basis", symbol=self.symbol, venues=("futures",),
            gross_edge_bps=gross_bps, fees_bps=fees, net_edge_bps=net_bps,
            confidence=0.8,  # basis converges to zero at expiry by construction
            reason=(
                f"dated future trades {basis:.2%} over spot with {days_to_expiry:.0f}d "
                f"to expiry = {apr:.1%} APR locked if held to expiry"
            ),
            action_hint="buy spot, short the dated future, hold to expiry",
            details={"spot": spot, "future": future, "days_to_expiry": days_to_expiry},
        )]


@dataclass
class DepegScanner:
    """Stablecoin deviation from $1. Detection only — the USDC-vs-UST judgment
    (temporary dislocation vs structural failure) stays with the human."""

    min_deviation_bps: float = 30.0
    taker_bps: float = 10.0

    def evaluate(self, prices: dict[str, float]) -> list[IntelFinding]:
        ts = _now()
        findings: list[IntelFinding] = []
        for coin, px in prices.items():
            dev_bps = (1.0 - px) * 10_000  # positive when below peg
            if abs(dev_bps) < self.min_deviation_bps:
                continue
            fees = 2 * self.taker_bps
            net_bps = abs(dev_bps) - fees
            if net_bps <= 0:
                continue
            below = dev_bps > 0
            findings.append(IntelFinding(
                ts=ts, kind="depeg", symbol=f"{coin}/USD",
                venues=("aggregate",),
                gross_edge_bps=abs(dev_bps), fees_bps=fees, net_edge_bps=net_bps,
                confidence=0.5,
                reason=(
                    f"{coin} is trading at ${px:.4f} "
                    f"({'below' if below else 'above'} peg by {abs(dev_bps):.0f} bps)"
                ),
                action_hint=(
                    "HUMAN DECISION: verify issuer solvency/redeemability first "
                    "(USDC-2023 recovered; UST did not)"
                ),
                details={"price": px, "deviation_bps": dev_bps},
            ))
        return findings


@dataclass
class WickScanner:
    """Wick-regime detector on recent OHLCV: are liquidation cascades happening
    NOW? Feeds context to the wick_catcher strategy and the dashboard."""

    flash_crash_pct: float = 0.05
    flash_crash_bars: int = 4
    wick_min_ratio: float = 2.5
    wick_window: int = 24
    wick_count: int = 3

    def evaluate(self, ohlcv: pd.DataFrame | None) -> list[IntelFinding]:
        if ohlcv is None or ohlcv.empty or len(ohlcv) < self.flash_crash_bars + 1:
            return []
        ts = _now()
        crash = flash_crash(
            ohlcv["close"], threshold_pct=self.flash_crash_pct, bars=self.flash_crash_bars,
        )
        wr = wick_ratio(ohlcv["open"], ohlcv["high"], ohlcv["low"], ohlcv["close"])
        wicks = wick_cluster(
            wr, window=self.wick_window, count=self.wick_count,
            ratio_min=self.wick_min_ratio,
        )
        crash_now = bool(crash.iloc[-1]) if len(crash) else False
        wicks_now = bool(wicks.iloc[-1]) if len(wicks) else False
        if not crash_now and not wicks_now:
            return []
        what = []
        if crash_now:
            what.append(f"price fell ≥{self.flash_crash_pct:.0%} in {self.flash_crash_bars} bars")
        if wicks_now:
            what.append(f"≥{self.wick_count} long-wick bars in last {self.wick_window}")
        return [IntelFinding(
            ts=ts, kind="wick_regime", symbol="BTC/USDT", venues=("market",),
            gross_edge_bps=0.0, fees_bps=0.0, net_edge_bps=0.0,
            confidence=0.9 if (crash_now and wicks_now) else 0.6,
            reason="liquidation-cascade regime active: " + "; ".join(what),
            action_hint="wick_catcher ladder is live for this; capitulation engine scoring",
            details={"flash_crash": crash_now, "wick_cluster": wicks_now},
        )]
