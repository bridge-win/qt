# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""S1 — Extreme-event response (v2.1): two entry modes, A/B tested in backtest.

confirm (live default): trade ONLY after the event tracker issues a REJECT
  verdict — the wick has begun to be unwound (reclaim confirmed, liquidation
  intensity decaying / flow flipping / local-venue dislocation). Entry is a
  marketable order with the slippage cap modeled in execution; stop sits
  beyond the event extreme + buffer; target is the event VWAP; time stop.
  Never fades an ACCEPTED (market-wide, holding) move; ambiguous = no trade.

ladder (legacy variant): passive post-only rungs resting inside the wick.
  Better prices, worse adverse selection. Kept for backtest A/B comparison.
"""
from __future__ import annotations

from ..config import S1Cfg
from ..models import Fill, Intent, MarketState, Side
from ..regime import PERMISSIONS, s1_side_allowed
from .base import Strategy


class S1WickCatcher(Strategy):
    name = "s1"

    def __init__(self, cfg: S1Cfg):
        super().__init__()
        self.cfg = cfg
        self.cooldown_until: int = 0
        # ladder-mode state
        self._ladder: dict[Side, list[float]] = {Side.BUY: [], Side.SELL: []}
        self._ladder_atr: float | None = None
        self._frozen = False
        # confirm-mode state
        self._traded_events: set[int] = set()
        self._planned_stop: float | None = None
        self._planned_tp: float | None = None

    # ------------------------------------------------------------- candle close

    def on_state(self, state: MarketState, equity: float) -> list[Intent]:
        if not self.cfg.enabled or not state.warmed_up:
            return []
        if self.cfg.entry_mode == "ladder":
            return self._ladder_on_state(state, equity)
        return self._confirm_on_state(state, equity)

    # =========================================================== confirm mode

    def _confirm_on_state(self, state: MarketState, equity: float) -> list[Intent]:
        if self.pos.is_flat():
            return self._confirm_entry(state, equity)
        intents: list[Intent] = []
        # thesis dead: the event flipped to market-wide acceptance -> get out now
        if state.event_active and state.event_verdict == "accept":
            intents.append(Intent("MARKET_EXIT", self.name, side=self.pos.side.opposite,
                                  qty=self.pos.qty, reason="verdict_flip"))
            return intents
        if self.pos.deadline_ts is not None and state.ts >= self.pos.deadline_ts:
            intents.append(Intent("MARKET_EXIT", self.name, side=self.pos.side.opposite,
                                  qty=self.pos.qty, reason="time_stop"))
        return intents

    def _confirm_entry(self, state: MarketState, equity: float) -> list[Intent]:
        if state.ts < self.cooldown_until:
            return []
        if not (state.event_active and state.event_verdict == "reject"):
            return []
        if state.event_id in self._traded_events:
            return []                                   # one trade per event
        if state.event_extreme is None or state.atr is None:
            return []
        side = Side.BUY if state.event_dir == "down" else Side.SELL
        if not s1_side_allowed(state.regime, side):
            return []
        buf = self.cfg.stop_buffer_atr * state.atr
        stop = state.event_extreme - buf if side is Side.BUY else state.event_extreme + buf
        dist = abs(state.price - stop)
        if dist <= 0:
            return []
        tp = state.event_vwap
        if tp is None or (side is Side.BUY and tp <= state.price) \
                or (side is Side.SELL and tp >= state.price):
            # VWAP already reclaimed or unavailable -> remaining edge too thin, skip
            return []
        mult = PERMISSIONS[state.regime].size_mult
        qty = equity * self.cfg.risk_per_trade * mult / dist
        self._planned_stop, self._planned_tp = stop, tp
        self._traded_events.add(state.event_id)
        cap = state.price * (1 + self.cfg.entry_slippage_bps / 10_000
                             if side is Side.BUY else
                             1 - self.cfg.entry_slippage_bps / 10_000)
        return [Intent("LIMIT_IOC_ENTER", self.name, side=side, qty=qty, price=cap,
                       reason=f"event_reject_{state.event_id}")]

    def _confirm_on_fill(self, fill: Fill, state: MarketState) -> list[Intent]:
        intents: list[Intent] = []
        if fill.kind == "MARKET" and not self.pos.is_flat():           # entry
            self.pos.deadline_ts = fill.ts + self.cfg.time_stop_min * 60_000
            side = self.pos.side
            stop = self._planned_stop if self._planned_stop is not None else fill.price * (
                0.99 if side is Side.BUY else 1.01)
            intents.append(Intent("PLACE_STOP", self.name, side=side.opposite,
                                  price=stop, qty=self.pos.qty, reason="hard_stop"))
            if self._planned_tp is not None:
                intents.append(Intent("PLACE_TP", self.name, side=side.opposite,
                                      price=self._planned_tp, qty=self.pos.qty,
                                      reason="event_vwap_target"))
        elif self.pos.is_flat():                                       # closed
            if fill.kind in ("STOP", "MARKET"):
                self.cooldown_until = fill.ts + self.cfg.cooldown_min * 60_000
            intents.append(Intent("CANCEL_ALL", self.name, reason="flat"))
            self._planned_stop = self._planned_tp = None
        return intents

    # =========================================================== ladder mode

    def _ladder_on_state(self, state: MarketState, equity: float) -> list[Intent]:
        if self.pos.is_flat():
            return self._flat_logic(state, equity)
        return self._managing_logic(state)

    def _flat_logic(self, state: MarketState, equity: float) -> list[Intent]:
        if state.ts < self.cooldown_until:
            return [Intent("CANCEL_LADDER", self.name, reason="cooldown")]
        perm = PERMISSIONS[state.regime]
        if not perm.s1_new_arming:
            return []
        intents: list[Intent] = []
        atr, ema, sigma = state.atr, state.ema_anchor, state.sigma_1m
        if atr is None or ema is None or sigma is None or atr <= 0:
            return []
        self._ladder_atr = atr
        self._frozen = False
        for side in (Side.BUY, Side.SELL):
            if not s1_side_allowed(state.regime, side):
                intents.append(Intent("CANCEL_LADDER", self.name, side=side, reason="regime"))
                self._ladder[side] = []
                continue
            prices, qtys = self._build_ladder(side, state, equity, perm.size_mult)
            self._ladder[side] = prices
            intents.append(Intent("REPLACE_LADDER", self.name, side=side,
                                  prices=prices, qtys=qtys, reason="arm"))
        return intents

    def _build_ladder(self, side: Side, state: MarketState, equity: float,
                      size_mult: float) -> tuple[list[float], list[float]]:
        atr, ema = state.atr, state.ema_anchor
        sgn = -1 if side is Side.BUY else 1
        prices = [ema + sgn * d * atr for d in self.cfg.rung_devs]
        stop = self._stop_price(side, prices, atr)
        risk_amount = equity * self.cfg.risk_per_rung * size_mult
        qtys = []
        for p in prices:
            dist = abs(p - stop)
            qtys.append(risk_amount / dist if dist > 0 else 0.0)
        return prices, qtys

    def _stop_price(self, side: Side, ladder: list[float], atr: float) -> float:
        deepest = min(ladder) if side is Side.BUY else max(ladder)
        return deepest - self.cfg.stop_atr_mult * atr if side is Side.BUY \
            else deepest + self.cfg.stop_atr_mult * atr

    def _managing_logic(self, state: MarketState) -> list[Intent]:
        intents: list[Intent] = []
        cascading = (state.liq_pctl is not None and state.liq_pctl >= self.cfg.freeze_liq_pctl
                     and state.doi_5m is not None and state.doi_5m <= self.cfg.freeze_doi_5m)
        if state.liq_pctl is None and state.vel5 is not None and abs(state.vel5) >= 6:
            cascading = True
        if cascading and not self._frozen:
            self._frozen = True
            intents.append(Intent("CANCEL_LADDER", self.name, reason="continuation_guard"))
        if self.pos.deadline_ts is not None and state.ts >= self.pos.deadline_ts:
            intents.append(Intent("MARKET_EXIT", self.name, side=self.pos.side.opposite,
                                  qty=self.pos.qty, reason="time_stop"))
        return intents

    def _ladder_on_fill(self, fill: Fill, state: MarketState) -> list[Intent]:
        intents: list[Intent] = []
        if fill.kind == "LIMIT":                       # a rung filled -> entry/add
            self.pos.rungs_filled += 1
            self.pos.deadline_ts = fill.ts + self.cfg.time_stop_min * 60_000
            side = self.pos.side
            intents.append(Intent("CANCEL_LADDER", self.name, side=side.opposite,
                                  reason="one_way"))
            ladder = self._ladder.get(side) or [fill.price]
            atr = self._ladder_atr or state.atr or 0.0
            stop = self._stop_price(side, ladder, atr)
            anchor = state.ema_anchor if state.ema_anchor is not None else fill.price
            tp = self.pos.avg_price + self.cfg.tp_revert_frac * (anchor - self.pos.avg_price)
            if side is Side.BUY:
                tp = max(tp, self.pos.avg_price * 1.0005)
            else:
                tp = min(tp, self.pos.avg_price * 0.9995)
            intents += [
                Intent("PLACE_TP", self.name, side=side.opposite, price=tp,
                       qty=self.pos.qty, reason="tp"),
                Intent("PLACE_STOP", self.name, side=side.opposite, price=stop,
                       qty=self.pos.qty, reason="hard_stop"),
            ]
            if self.pos.rungs_filled >= self.cfg.max_rungs_filled:
                intents.append(Intent("CANCEL_LADDER", self.name, reason="max_rungs"))
        elif self.pos.is_flat():
            if fill.kind in ("STOP", "MARKET"):
                self.cooldown_until = fill.ts + self.cfg.cooldown_min * 60_000
            intents.append(Intent("CANCEL_ALL", self.name, reason="flat"))
            self._frozen = False
        return intents

    # ------------------------------------------------------------- fills

    def on_fill(self, fill: Fill, state: MarketState) -> tuple[list[Intent], float]:
        pnl = self.pos.apply_fill(fill)
        if self.cfg.entry_mode == "ladder":
            return self._ladder_on_fill(fill, state), pnl
        return self._confirm_on_fill(fill, state), pnl

