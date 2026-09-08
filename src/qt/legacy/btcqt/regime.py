# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""S3 regime state machine (C3) and the strategy permission matrix (v2 plan §5)."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Regime, Side, Trend


class RegimeTracker:
    """Holds cascade memory: CASCADE while score >= threshold,
    then POST_CASCADE for `post_window_min` minutes after it ends."""

    def __init__(self, cascade_threshold: float, post_window_min: int,
                 adx_strong: float, rv_pctl_max_for_trend: float = 90.0):
        self.threshold = cascade_threshold
        self.post_window_ms = post_window_min * 60_000
        self.adx_strong = adx_strong
        self.rv_pctl_max_for_trend = rv_pctl_max_for_trend
        self._in_cascade = False
        self._last_cascade_end: int | None = None

    def update(self, ts: int, cascade: float, trend: Trend, rv_pctl: float | None) -> Regime:
        if cascade >= self.threshold:
            self._in_cascade = True
            return Regime.CASCADE
        if self._in_cascade:                       # cascade just ended
            self._in_cascade = False
            self._last_cascade_end = ts
        if self._last_cascade_end is not None and ts - self._last_cascade_end <= self.post_window_ms:
            return Regime.POST_CASCADE
        rv_ok = rv_pctl is None or rv_pctl < self.rv_pctl_max_for_trend
        if trend is Trend.STRONG_UP and rv_ok:
            return Regime.TREND_UP
        if trend is Trend.STRONG_DOWN and rv_ok:
            return Regime.TREND_DOWN
        return Regime.RANGE


@dataclass(frozen=True)
class Permission:
    s0: bool
    s1_buy: bool          # catch downward wicks (long)
    s1_sell: bool         # catch upward wicks (short)
    s1_new_arming: bool   # may place NEW ladders (existing rungs may still fill)
    s2: bool
    size_mult: float


PERMISSIONS: dict[Regime, Permission] = {
    Regime.RANGE:        Permission(True, True,  True,  True,  True,  1.0),
    Regime.POST_CASCADE: Permission(True, True,  True,  True,  True,  0.7),
    Regime.TREND_UP:     Permission(True, True,  False, True,  False, 0.7),
    Regime.TREND_DOWN:   Permission(True, False, True,  True,  False, 0.7),
    Regime.CASCADE:      Permission(False, True, True,  False, False, 0.5),
}


def s1_side_allowed(regime: Regime, side: Side) -> bool:
    p = PERMISSIONS[regime]
    return p.s1_buy if side is Side.BUY else p.s1_sell

