# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""S0 — Volatility-adjusted trend following (plan v2.1 strategy A, the core sleeve).

Entry: multi-horizon vol-normalized trend score T with >=2 horizons agreeing,
confirmed by a 1h Donchian(20) break in the same direction.
Exit: hard stop 2.5×ATR(1h) placed on the exchange, opposite Donchian break,
or trend-score reversal. Size halves when realized vol is extreme (vol targeting).
Loses small in chop by design; earns in persistent moves.
"""
from __future__ import annotations

from ..config import S0Cfg
from ..models import Fill, Intent, MarketState, Side
from ..regime import PERMISSIONS
from .base import Strategy


class S0Trend(Strategy):
    name = "s0"

    def __init__(self, cfg: S0Cfg):
        super().__init__()
        self.cfg = cfg
        self.cooldown_until = 0

    def on_state(self, state: MarketState, equity: float) -> list[Intent]:
        if not self.cfg.enabled or not state.warmed_up:
            return []
        if state.trend_score is None or state.atr_1h is None:
            return []
        if self.pos.is_flat():
            return self._entry(state, equity)
        return self._exit(state)

    # ------------------------------------------------------------- entry

    def _entry(self, state: MarketState, equity: float) -> list[Intent]:
        if state.ts < self.cooldown_until:
            return []
        if not PERMISSIONS[state.regime].s0:
            return []
        t = state.trend_score
        if abs(t) < self.cfg.min_abs_t or state.trend_agree < self.cfg.min_agree:
            return []
        side: Side | None = None
        if t > 0 and state.donchian_break == 1:
            side = Side.BUY
        elif t < 0 and state.donchian_break == -1:
            side = Side.SELL
        if side is None:
            return []
        stop_dist = self.cfg.stop_atr1h_mult * state.atr_1h
        if stop_dist <= 0:
            return []
        mult = PERMISSIONS[state.regime].size_mult
        if state.rv_pctl is not None and state.rv_pctl >= self.cfg.high_vol_rv_pctl:
            mult *= self.cfg.high_vol_size_mult          # vol targeting
        qty = equity * self.cfg.risk_per_trade * mult / stop_dist
        return [Intent("MARKET_ENTER", self.name, side=side, qty=qty, reason="trend_break")]

    # ------------------------------------------------------------- exit

    def _exit(self, state: MarketState) -> list[Intent]:
        assert self.pos.side is not None
        long = self.pos.side is Side.BUY
        t = state.trend_score or 0.0
        reversed_trend = (state.trend_agree >= self.cfg.min_agree
                          and ((long and t < -self.cfg.min_abs_t)
                               or (not long and t > self.cfg.min_abs_t)))
        opposite_break = (long and state.donchian_break == -1) or \
                         (not long and state.donchian_break == 1)
        if reversed_trend or opposite_break:
            return [Intent("MARKET_EXIT", self.name, side=self.pos.side.opposite,
                           qty=self.pos.qty, reason="trend_reversal")]
        return []

    # ------------------------------------------------------------- fills

    def on_fill(self, fill: Fill, state: MarketState) -> tuple[list[Intent], float]:
        pnl = self.pos.apply_fill(fill)
        intents: list[Intent] = []
        if fill.kind == "MARKET" and not self.pos.is_flat():          # entry
            atr = state.atr_1h or 0.0
            stop = (fill.price - self.cfg.stop_atr1h_mult * atr if self.pos.side is Side.BUY
                    else fill.price + self.cfg.stop_atr1h_mult * atr)
            intents.append(Intent("PLACE_STOP", self.name, side=self.pos.side.opposite,
                                  price=stop, qty=self.pos.qty, reason="hard_stop"))
        elif self.pos.is_flat():                                       # closed
            if fill.kind == "STOP":
                self.cooldown_until = fill.ts + self.cfg.cooldown_min * 60_000
            intents.append(Intent("CANCEL_ALL", self.name, reason="flat"))
        return intents, pnl

