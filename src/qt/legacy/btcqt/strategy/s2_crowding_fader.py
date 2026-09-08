# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""S2 — Crowding Fader (v2 plan §5).

Fades extreme, persistent funding-rate crowding (with OI confirmation and a
price-exhaustion trigger). Small position, hard stop, time stop; the natural
exit is crowding dissipating (funding percentile normalizing). Position is
on the side that RECEIVES funding while it waits.
"""
from __future__ import annotations

from ..config import S2Cfg
from ..models import Fill, Intent, MarketState, Regime, Side
from ..regime import PERMISSIONS
from .base import Strategy


class S2CrowdingFader(Strategy):
    name = "s2"

    def __init__(self, cfg: S2Cfg):
        super().__init__()
        self.cfg = cfg
        self._hi_streak = 0          # consecutive settlements at extreme (longs crowded)
        self._lo_streak = 0
        self._last_funding_bucket: int | None = None

    def on_state(self, state: MarketState, equity: float) -> list[Intent]:
        if not self.cfg.enabled or not state.warmed_up:
            return []
        self._update_streaks(state)
        if self.pos.is_flat():
            return self._entry_logic(state, equity)
        return self._exit_logic(state)

    # ------------------------------------------------------------- persistence

    def _update_streaks(self, state: MarketState) -> None:
        """Count consecutive 8h funding buckets at the extreme percentile."""
        if state.funding_pctl is None:
            return
        bucket = state.ts // (8 * 3600 * 1000)
        if bucket == self._last_funding_bucket:
            return
        self._last_funding_bucket = bucket
        if state.funding_pctl >= self.cfg.funding_pctl_hi:
            self._hi_streak += 1
            self._lo_streak = 0
        elif state.funding_pctl <= self.cfg.funding_pctl_lo:
            self._lo_streak += 1
            self._hi_streak = 0
        else:
            self._hi_streak = self._lo_streak = 0

    # ------------------------------------------------------------- entry/exit

    def _entry_logic(self, state: MarketState, equity: float) -> list[Intent]:
        if not PERMISSIONS[state.regime].s2:
            return []
        if (state.atr_tf is None or state.dev is None or state.vel5 is None
                or state.open_interest_pctl is None
                or state.open_interest_pctl < self.cfg.oi_pctl_min):
            return []
        side: Side | None = None
        if self._hi_streak >= self.cfg.min_persist_intervals:
            # longs crowded -> fade short, but only on exhaustion (stretched up, momentum stalled)
            if state.dev > 0.5 and state.vel5 <= 0:
                side = Side.SELL
        elif self._lo_streak >= self.cfg.min_persist_intervals:
            if state.dev < -0.5 and state.vel5 >= 0:
                side = Side.BUY
        if side is None:
            return []
        stop_dist = self.cfg.stop_atr4h_mult * state.atr_tf
        if stop_dist <= 0:
            return []
        qty = equity * self.cfg.risk_per_trade * PERMISSIONS[state.regime].size_mult / stop_dist
        return [Intent("MARKET_ENTER", self.name, side=side, qty=qty, reason="crowding_fade")]

    def _exit_logic(self, state: MarketState) -> list[Intent]:
        assert self.pos.side is not None
        # crowding dissipated -> thesis complete
        if state.funding_pctl is not None and abs(state.funding_pctl - 50.0) < (self.cfg.funding_exit_pctl - 50.0):
            return [Intent("MARKET_EXIT", self.name, side=self.pos.side.opposite,
                           qty=self.pos.qty, reason="crowding_gone")]
        # regime turned into an opposing strong trend
        if ((self.pos.side is Side.SELL and state.regime is Regime.TREND_UP)
                or (self.pos.side is Side.BUY and state.regime is Regime.TREND_DOWN)):
            return [Intent("MARKET_EXIT", self.name, side=self.pos.side.opposite,
                           qty=self.pos.qty, reason="regime_flip")]
        # time stop
        if self.pos.deadline_ts is not None and state.ts >= self.pos.deadline_ts:
            return [Intent("MARKET_EXIT", self.name, side=self.pos.side.opposite,
                           qty=self.pos.qty, reason="time_stop")]
        return []

    # ------------------------------------------------------------- fills

    def on_fill(self, fill: Fill, state: MarketState) -> tuple[list[Intent], float]:
        pnl = self.pos.apply_fill(fill)
        intents: list[Intent] = []
        if fill.kind == "MARKET" and not self.pos.is_flat():   # entry
            self.pos.deadline_ts = fill.ts + self.cfg.time_stop_hours * 3600 * 1000
            atr = state.atr_tf or 0.0
            stop = (fill.price - self.cfg.stop_atr4h_mult * atr if self.pos.side is Side.BUY
                    else fill.price + self.cfg.stop_atr4h_mult * atr)
            intents.append(Intent("PLACE_STOP", self.name, side=self.pos.side.opposite,
                                  price=stop, qty=self.pos.qty, reason="hard_stop"))
            self._hi_streak = self._lo_streak = 0
        elif self.pos.is_flat():
            intents.append(Intent("CANCEL_ALL", self.name, reason="flat"))
        return intents, pnl

