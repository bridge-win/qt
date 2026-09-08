# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Extreme-event tracker with rejection/acceptance verdict (plan v2.1 §0, review §3).

An event OPENS when the trigger fires (abnormal 1m return + forced-flow /
deleverage evidence when available). While open, the tracker maintains the
event VWAP, the extreme, the pre-event reference, and the reclaim fraction,
and classifies the event each minute:

  reject    local dislocation being unwound -> the ONLY state strategy B may trade
  accept    market-wide repricing that holds -> never fade; trend engine's turf
  pending   too early to call
  ambiguous evidence conflicts or expired unresolved -> no trade

Degradation rule (kline-only backtests): when liquidation/taker/cross-venue
inputs are all unavailable, a REJECT verdict requires a STRICTER reclaim
(reclaim_min_degraded) — never looser. Missing data must make the system more
conservative, not more active.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import Candle


@dataclass
class EventConfig:
    z_trigger: float = 3.0            # robust-z threshold on 1m return
    liq_oi_pctl_trigger: float = 99.0
    doi_pctl_trigger: float = 1.0     # 5m OI change below its p1 (deleverage flush)
    pre_window_min: int = 60          # pre-event reference window
    max_age_min: int = 120            # event expires
    reclaim_min: float = 0.30         # reject needs >=30% reclaim (full data)
    reclaim_min_degraded: float = 0.40
    xvenue_dev_big: float = 1.0       # |ATR units| counted as a local dislocation
    accept_hold_min: int = 10         # price holding beyond range this long -> accept
    one_verdict_per_event: bool = True


@dataclass
class EventState:
    active: bool = False
    event_id: int = 0
    direction: str = "none"           # down | up
    open_ts: int = 0
    extreme: float = 0.0
    pre_ref: float = 0.0              # pre-event reference price (median of window)
    range_edge: float = 0.0           # pre-event range edge on the event side
    vwap_num: float = 0.0
    vwap_den: float = 0.0
    xvenue_at_trigger: float | None = None
    peak_liq: float = 0.0
    verdict: str = "none"
    beyond_range_min: int = 0         # consecutive minutes closing beyond range edge

    @property
    def vwap(self) -> float | None:
        return self.vwap_num / self.vwap_den if self.vwap_den > 0 else None


class EventTracker:
    def __init__(self, cfg: EventConfig | None = None):
        self.cfg = cfg or EventConfig()
        self.st = EventState()
        self._pre_prices: list[float] = []     # rolling pre-event window of closes
        self._pre_lows: list[float] = []
        self._pre_highs: list[float] = []
        self._counter = 0

    # ------------------------------------------------------------ per-candle

    def update(self, c: Candle, *, ret_z: float | None, liq_oi_pctl: float | None,
               doi_pctl: float | None, liq_notional_60s: float,
               liq_decaying: bool | None, taker_imbalance: float | None,
               xvenue_dev: float | None) -> EventState:
        if not self.st.active:
            self._maybe_open(c, ret_z, liq_oi_pctl, doi_pctl, xvenue_dev)
        else:
            self._update_open(c, liq_notional_60s, liq_decaying, taker_imbalance)
        # maintain pre-event window only while no event is running
        if not self.st.active:
            self._pre_prices.append(c.close)
            self._pre_lows.append(c.low)
            self._pre_highs.append(c.high)
            if len(self._pre_prices) > self.cfg.pre_window_min:
                self._pre_prices.pop(0)
                self._pre_lows.pop(0)
                self._pre_highs.pop(0)
        return self.st

    def reclaim_frac(self, price: float) -> float | None:
        s = self.st
        if not s.active and s.verdict == "none":
            return None
        span = s.pre_ref - s.extreme
        if abs(span) < 1e-12:
            return None
        return (price - s.extreme) / span      # same sign for up & down events

    # ------------------------------------------------------------ internals

    def _maybe_open(self, c: Candle, ret_z, liq_oi_pctl, doi_pctl, xvenue_dev) -> None:
        if ret_z is None or abs(ret_z) < self.cfg.z_trigger:
            return
        if len(self._pre_prices) < self.cfg.pre_window_min // 2:
            return
        # forced-flow / deleverage corroboration when the feeds exist
        if liq_oi_pctl is not None and doi_pctl is not None:
            if liq_oi_pctl < self.cfg.liq_oi_pctl_trigger and doi_pctl > self.cfg.doi_pctl_trigger:
                return                          # abnormal print without forced flow: ignore
        down = ret_z < 0
        pre_sorted = sorted(self._pre_prices)
        self._counter += 1
        self.st = EventState(
            active=True, event_id=self._counter,
            direction="down" if down else "up",
            open_ts=c.close_ts,
            extreme=c.low if down else c.high,
            pre_ref=pre_sorted[len(pre_sorted) // 2],
            range_edge=min(self._pre_lows) if down else max(self._pre_highs),
            xvenue_at_trigger=xvenue_dev,
            verdict="pending",
        )
        self._ingest(c)

    def _update_open(self, c: Candle, liq_notional_60s, liq_decaying, taker_imbalance) -> None:
        s, cfg = self.st, self.cfg
        self._ingest(c)
        down = s.direction == "down"
        s.extreme = min(s.extreme, c.low) if down else max(s.extreme, c.high)
        s.peak_liq = max(s.peak_liq, liq_notional_60s)
        beyond = c.close < s.range_edge if down else c.close > s.range_edge
        s.beyond_range_min = s.beyond_range_min + 1 if beyond else 0
        age_min = (c.close_ts - s.open_ts) // 60_000

        if s.verdict == "pending" or not cfg.one_verdict_per_event:
            s.verdict = self._classify(c, liq_decaying, taker_imbalance, age_min)
        if age_min >= cfg.max_age_min:
            if s.verdict == "pending":
                s.verdict = "ambiguous"
            s.active = False

    def _classify(self, c: Candle, liq_decaying, taker_imbalance, age_min: int) -> str:
        s, cfg = self.st, self.cfg
        down = s.direction == "down"
        rf = self.reclaim_frac(c.close) or 0.0

        # ---- acceptance: the move HOLDS beyond the pre-event range with flow support
        flow_continues = ((taker_imbalance is not None)
                          and ((down and taker_imbalance < -0.2)
                               or (not down and taker_imbalance > 0.2)))
        liq_still_running = liq_decaying is False
        if s.beyond_range_min >= cfg.accept_hold_min and (flow_continues or liq_still_running or rf < 0.1):
            return "accept"

        # ---- rejection: dislocation being unwound
        aux: list[bool] = []
        if s.xvenue_at_trigger is not None:
            aux.append(abs(s.xvenue_at_trigger) >= cfg.xvenue_dev_big)
        if liq_decaying is not None:
            aux.append(bool(liq_decaying))
        if taker_imbalance is not None:
            aux.append((down and taker_imbalance > 0.1) or (not down and taker_imbalance < -0.1))
        if aux:                                          # full-data path
            if rf >= cfg.reclaim_min and any(aux) and not flow_continues:
                return "reject"
        else:                                            # degraded (kline-only) path
            if rf >= cfg.reclaim_min_degraded:
                return "reject"
        return "pending" if age_min < cfg.max_age_min else "ambiguous"

    def _ingest(self, c: Candle) -> None:
        typical = (c.high + c.low + c.close) / 3.0
        self.st.vwap_num += typical * c.volume
        self.st.vwap_den += c.volume


