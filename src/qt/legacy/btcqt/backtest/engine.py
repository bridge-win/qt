# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Event-driven backtester. Reuses the SAME IndicatorEngine, strategies and
RiskManager as live/paper — only the executor (SimBook) differs.

Per-candle ordering (no lookahead):
  1. inject funding settlements / OI snapshots timestamped inside this candle
  2. fill orders that were placed at previous closes, using this candle's OHLC
  3. route fills -> strategy.on_fill -> risk accounting -> follow-up orders
  4. close the candle in the indicator engine -> MarketState
  5. strategies decide -> risk filter -> orders for the NEXT candle
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import Config
from ..indicators.engine import IndicatorEngine
from ..models import Candle, Fill, FundingRate, Intent, OISnapshot, Side, Trade, intent_notional
from ..risk import RiskManager
from ..strategy.base import Strategy
from ..strategy.s0_trend import S0Trend
from ..strategy.s1_wick_catcher import S1WickCatcher
from ..strategy.s2_crowding_fader import S2CrowdingFader
from .fills import SimBook

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    equity_ts: list[int] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    risk_events: list = field(default_factory=list)
    states_sample: list[dict] = field(default_factory=list)
    funding_pnl: float = 0.0
    final_equity: float = 0.0


class _TradeTracker:
    """Aggregates fills into round-trip Trade records per strategy."""

    def __init__(self):
        self._open: dict[str, dict] = {}

    def on_fill(self, f: Fill, position_flat_after: bool, realized: float,
                position_side_after: Side | None = None) -> Trade | None:
        st = self._open.setdefault(f.strategy, {
            "side": None, "entry_ts": 0, "qty": 0.0, "entry_notional": 0.0,
            "fees": 0.0, "pnl": 0.0})
        st["fees"] += f.fee
        if not position_flat_after and f.side is position_side_after:  # entry/add
            if st["side"] is None:
                st["side"] = f.side.value
                st["entry_ts"] = f.ts
            st["qty"] += f.qty
            st["entry_notional"] += f.price * f.qty
            return None
        st["pnl"] += realized
        if position_flat_after and st["side"] is not None:
            qty = st["qty"] or f.qty
            entry_px = st["entry_notional"] / qty if qty else f.price
            tr = Trade(strategy=f.strategy, side=st["side"], entry_ts=st["entry_ts"],
                       exit_ts=f.ts, entry_price=entry_px, exit_price=f.price, qty=qty,
                       pnl=st["pnl"] - st["fees"], fees=st["fees"],
                       exit_kind=f.tag if f.kind == "MARKET" and f.tag else f.kind)
            self._open[f.strategy] = {"side": None, "entry_ts": 0, "qty": 0.0,
                                      "entry_notional": 0.0, "fees": 0.0, "pnl": 0.0}
            return tr
        return None

    def on_carry(self, strategy: str, pnl: float) -> None:
        """Attach funding cash flow to the open round trip for honest attribution."""
        state = self._open.get(strategy)
        if state is not None and state["side"] is not None:
            state["pnl"] += pnl


class Backtester:
    def __init__(self, cfg: Config, klines: pd.DataFrame,
                 funding: pd.DataFrame | None = None,
                 oi: pd.DataFrame | None = None,
                 collect_states: bool = False,
                 trade_start_ms: int | None = None):
        self.cfg = cfg
        self.klines = klines
        self.funding = funding if funding is not None else pd.DataFrame(columns=["ts", "rate"])
        self.oi = oi if oi is not None else pd.DataFrame(columns=["ts", "oi"])
        self.collect_states = collect_states
        self.trade_start_ms = trade_start_ms
        self.engine = IndicatorEngine(cfg)
        self.book = SimBook(cfg.account.maker_fee, cfg.account.taker_fee,
                            cfg.account.stop_slippage_bps)
        self.risk = RiskManager(cfg.risk, cfg.account.initial_equity)
        self.strategies: list[Strategy] = []
        if cfg.s0.enabled:
            self.strategies.append(S0Trend(cfg.s0))
        if cfg.s1.enabled:
            self.strategies.append(S1WickCatcher(cfg.s1))
        if cfg.s2.enabled:
            self.strategies.append(S2CrowdingFader(cfg.s2))
        self._tracker = _TradeTracker()
        self.trades: list[Trade] = []
        self.funding_pnl = 0.0

    # ------------------------------------------------------------ main loop

    def run(self) -> BacktestResult:
        res = BacktestResult()
        first_ts = int(self.klines.ts.iloc[0]) if len(self.klines) else 0
        pre = self.funding[self.funding.ts < first_ts]
        self.engine.preload_funding(
            [FundingRate(int(r.ts), float(r.rate)) for r in pre.itertuples()])
        fund_iter = self.funding[self.funding.ts >= first_ts].itertuples()
        oi_iter = self.oi.itertuples()
        next_fund = next(fund_iter, None)
        next_oi = next(oi_iter, None)
        arr = self.klines[["ts", "open", "high", "low", "close", "volume"]].to_numpy()
        last_candle: Candle | None = None

        for row in arr:
            c = Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                       float(row[4]), float(row[5]))
            last_candle = c
            # 1) time-ordered exogenous events inside this candle
            while next_fund is not None and next_fund.ts <= c.close_ts:
                fr = FundingRate(int(next_fund.ts), float(next_fund.rate))
                self.engine.on_funding_settled(fr)
                self._apply_funding(fr, c.close)
                next_fund = next(fund_iter, None)
            while next_oi is not None and next_oi.ts <= c.close_ts:
                self.engine.on_oi(OISnapshot(int(next_oi.ts), float(next_oi.oi)))
                next_oi = next(oi_iter, None)

            # 2-3) fills from orders resting since previous close
            state_before = self.engine.last_state
            for f in self.book.process_candle(c):
                self._route_fill(f, state_before)

            # 4) close candle -> state
            state = self.engine.on_candle(c)
            if self.collect_states and state.warmed_up and (
                    state.cascade_score >= 50 or (state.dev is not None and abs(state.dev) >= 2.5)):
                res.states_sample.append(state.to_dict())

            # 5) mark risk before any new decision, then decide -> risk -> book
            trading_window = self.trade_start_ms is None or state.ts >= self.trade_start_ms
            if state.warmed_up and trading_window:
                new_halts = self.risk.on_mark(
                    state.ts, self.risk.equity + self._unrealized(c.close))
                if any(e.kind in ("HALT_DAY", "HALT_WEEK", "HALT_SYSTEM_DD", "KILL")
                       for e in new_halts):
                    self._flatten_all("risk_halt")
                gross = self._gross_notional(c.close)
                portfolio_sides = {x.pos.side for x in self.strategies
                                   if not x.pos.is_flat() and x.pos.side is not None}
                for s in self.strategies:
                    intents = s.on_state(state, self.risk.sizing_equity)
                    approved = self.risk.filter_intents(intents, state.ts, c.close, gross,
                                                        spread_pctl=state.spread_pctl,
                                                        open_sides=portfolio_sides)
                    self._apply_intents(approved)
                    gross += sum(intent_notional(it, c.close) for it in approved)
                    portfolio_sides.update(it.side for it in approved
                                           if it.action in ("MARKET_ENTER", "LIMIT_IOC_ENTER")
                                           and it.side is not None)

            if trading_window:
                eq = self.risk.equity + self._unrealized(c.close)
                res.equity_ts.append(c.close_ts)
                res.equity.append(eq)

        if last_candle is not None:
            self._close_at_end(last_candle)
            if res.equity:
                res.equity[-1] = self.risk.equity
        res.trades = self.trades
        res.risk_events = self.risk.events
        res.funding_pnl = self.funding_pnl
        res.final_equity = res.equity[-1] if res.equity else self.cfg.account.initial_equity
        return res

    # ------------------------------------------------------------ helpers

    def _route_fill(self, f: Fill, state) -> None:
        strat = next((s for s in self.strategies if s.name == f.strategy), None)
        if strat is None:
            return
        intents, realized = strat.on_fill(f, state or self.engine.last_state)
        # equity accounting: entry fees at entry, pnl-net-of-exit-fee at exit
        self.risk.on_realized(f.ts, realized - f.fee)
        tr = self._tracker.on_fill(f, strat.pos.is_flat(), realized, strat.pos.side)
        if tr is not None:
            self.trades.append(tr)
        self._apply_intents(intents)

    def _apply_intents(self, intents: list[Intent]) -> None:
        for it in intents:
            b = self.book
            if it.action == "REPLACE_LADDER":
                b.cancel_kind(it.strategy, "LIMIT", side=it.side)
                for p, q in zip(it.prices, it.qtys):
                    if q > 0:
                        b.place_limit(it.strategy, it.side, p, q)
            elif it.action == "CANCEL_LADDER":
                b.cancel_kind(it.strategy, "LIMIT", side=it.side)
            elif it.action == "CANCEL_ALL":
                b.cancel_all(it.strategy)
            elif it.action == "PLACE_TP":
                b.place_tp(it.strategy, it.side, it.price, it.qty)
            elif it.action == "PLACE_STOP":
                b.place_stop(it.strategy, it.side, it.price, it.qty)
            elif it.action in ("MARKET_EXIT", "MARKET_ENTER"):
                b.market(it.strategy, it.side, it.qty, it.reason)
            elif it.action == "LIMIT_IOC_ENTER":
                b.ioc(it.strategy, it.side, it.qty, it.price, it.reason)

    def _apply_funding(self, fr: FundingRate, mark: float) -> None:
        """Long pays when funding positive; short receives. Applied to open positions."""
        for s in self.strategies:
            if s.pos.is_flat():
                continue
            pnl = -s.pos.side.sign * fr.rate * s.pos.qty * mark
            self.risk.on_realized(fr.ts, pnl)
            self._tracker.on_carry(s.name, pnl)
            self.funding_pnl += pnl

    def _unrealized(self, price: float) -> float:
        u = 0.0
        for s in self.strategies:
            if not s.pos.is_flat():
                u += (price - s.pos.avg_price) * s.pos.qty * s.pos.side.sign
        return u

    def _gross_notional(self, price: float) -> float:
        return sum(s.pos.qty * price for s in self.strategies if not s.pos.is_flat())

    def _flatten_all(self, reason: str) -> None:
        for s in self.strategies:
            self.book.cancel_all(s.name)
            if not s.pos.is_flat():
                already = any(pm.strategy == s.name for pm in self.book.pending_market)
                if not already:
                    self.book.market(s.name, s.pos.side.opposite, s.pos.qty, reason)

    def _close_at_end(self, candle: Candle) -> None:
        """Realize open positions at the final close; never report free unrealized PnL."""
        self.book.orders.clear()
        self.book.pending_market.clear()
        slip = self.book.slippage_bps / 10_000
        for strategy in self.strategies:
            if strategy.pos.is_flat():
                continue
            side = strategy.pos.side.opposite
            price = candle.close * (1 + slip if side is Side.BUY else 1 - slip)
            fill = Fill(ts=candle.close_ts, order_id=f"{strategy.name}-END",
                        strategy=strategy.name, side=side, price=price,
                        qty=strategy.pos.qty, fee=price * strategy.pos.qty * self.book.taker_fee,
                        kind="MARKET", tag="end_of_backtest")
            self._route_fill(fill, self.engine.last_state)

