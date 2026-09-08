# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Indicator engine: consumes market events, maintains B-layer indicators
incrementally, and emits a full MarketState snapshot at every 1m candle close.

The SAME engine runs in backtest, paper, and live — this is the core guarantee
against backtest-vs-live drift (v2 plan §7).
"""
from __future__ import annotations

import math
from collections import deque

from ..config import Config
from ..models import (BookTicker, Candle, ForceOrder, FundingRate, MarkPrice,
                      MarketState, OISnapshot, Trend)
from ..regime import RegimeTracker
from ..util.rolling import Atr, Ewma, EwmVol, RollingWindow, TimedWindow
from . import composite
from .events import EventConfig, EventTracker


class _Adx:
    """Incremental Wilder ADX over higher-timeframe candles."""

    def __init__(self, period: int):
        self.period = period
        self._prev: Candle | None = None
        self._tr = self._pdm = self._ndm = None      # smoothed
        self._warm_tr: list[float] = []
        self._warm_pdm: list[float] = []
        self._warm_ndm: list[float] = []
        self._adx: float | None = None
        self._warm_dx: list[float] = []

    @property
    def value(self) -> float | None:
        return self._adx

    def update(self, c: Candle) -> float | None:
        if self._prev is None:
            self._prev = c
            return None
        up = c.high - self._prev.high
        down = self._prev.low - c.low
        pdm = up if (up > down and up > 0) else 0.0
        ndm = down if (down > up and down > 0) else 0.0
        tr = max(c.high - c.low, abs(c.high - self._prev.close), abs(c.low - self._prev.close))
        self._prev = c
        if self._tr is None:
            self._warm_tr.append(tr); self._warm_pdm.append(pdm); self._warm_ndm.append(ndm)
            if len(self._warm_tr) < self.period:
                return None
            self._tr, self._pdm, self._ndm = sum(self._warm_tr), sum(self._warm_pdm), sum(self._warm_ndm)
        else:
            self._tr = self._tr - self._tr / self.period + tr
            self._pdm = self._pdm - self._pdm / self.period + pdm
            self._ndm = self._ndm - self._ndm / self.period + ndm
        if self._tr <= 0:
            return self._adx
        pdi = 100.0 * self._pdm / self._tr
        ndi = 100.0 * self._ndm / self._tr
        s = pdi + ndi
        dx = 100.0 * abs(pdi - ndi) / s if s > 0 else 0.0
        if self._adx is None:
            self._warm_dx.append(dx)
            if len(self._warm_dx) >= self.period:
                self._adx = sum(self._warm_dx) / self.period
        else:
            self._adx = (self._adx * (self.period - 1) + dx) / self.period
        return self._adx


class IndicatorEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        ic = cfg.indicators
        # B1/B2/B3 building blocks
        self._closes: deque[float] = deque(maxlen=6)            # for VEL(1/3/5)
        self._sigma = EwmVol(ic.sigma_span_1m)
        self._ema_anchor = Ewma(ic.ema_anchor_min)
        self._atr = Atr(ic.atr_period_1m)
        self._vols = RollingWindow(ic.vb_window)
        # B4 liquidations
        self._liq_win = TimedWindow(ic.liq_window_sec * 1000)
        self._liq_minute = RollingWindow(ic.liq_pctl_lookback_min)
        self._liq_24h = TimedWindow(24 * 3600 * 1000)
        self._liq_24h_hist = RollingWindow(30 * 24)             # hourly 24h-notional history, ~30d
        self._candles_since_liq_hist = 0
        # B5 open interest
        self._oi: deque[OISnapshot] = deque(maxlen=4000)
        self._oi_pctl_win = RollingWindow(30 * 24 * 12)         # ~30d of 5m samples
        # B6 funding
        self._funding_pred: float | None = None
        self._funding_30d = RollingWindow(ic.funding_pctl_days * 3)   # 3 settlements/day
        self._funding_90d = RollingWindow(90 * 3)
        # B7 realized vol
        self._r2 = RollingWindow(ic.rv_window_min)
        self._rv_hist = RollingWindow(ic.rv_pctl_lookback_days * 24)  # hourly snapshots
        # B8 trend (higher timeframe)
        self._tf_ms = ic.trend_tf_min * 60_000
        self._tf_bucket: int | None = None
        self._tf_candle: Candle | None = None
        self._ema50 = Ewma(50)
        self._ema200 = Ewma(200)
        self._ema50_hist: deque[float] = deque(maxlen=6)
        self._adx = _Adx(ic.adx_period)
        self._atr_tf = Atr(14)
        self._tf_count = 0
        # B9 basis
        self._basis_win = RollingWindow(8640)                    # ~30d of 5m samples
        self._last_mark: MarkPrice | None = None
        # A9 fear&greed / optional long-short ratio z
        self._fng: float | None = None
        self._lsr_win = RollingWindow(8640)
        self._lsr_last: float | None = None
        # ---- v0.2.0 additions (plan v2.1) ----
        # 1h aggregation: trend horizons, Donchian, ATR(14,1h)
        self._h1_ms = 3_600_000
        self._h1_bucket: int | None = None
        self._h1_candle: Candle | None = None
        self._h1: deque[Candle] = deque(maxlen=400)          # completed 1h candles
        self._h1_ret_win = RollingWindow(336)                 # hourly log returns, 14d
        self._atr_1h = Atr(14)
        self._donchian_break = 0
        # robust-z of 1m returns
        self._ret_win = RollingWindow(1440)
        # liquidation/OI ratio + decay
        self._liq_oi_minute = RollingWindow(1440)
        self._liq_recent: deque[float] = deque(maxlen=10)     # per-minute liq sums
        # ΔOI percentile
        self._doi_win = RollingWindow(2000)
        # cross-venue reference prices
        self._xprices: dict[str, tuple[int, float]] = {}
        # taker flow (5m rolling)
        self._taker_buy = TimedWindow(5 * 60_000)
        self._taker_sell = TimedWindow(5 * 60_000)
        # spread guard
        self._last_book: BookTicker | None = None
        self._spread_win = RollingWindow(2880)
        # extreme-event tracker (rejection/acceptance)
        self._events = EventTracker(EventConfig(**cfg.event.model_dump()))
        # C3 regime
        self._regime = RegimeTracker(cfg.composite.cascade_threshold,
                                     cfg.composite.post_cascade_min,
                                     ic.adx_strong)
        self._candles_seen = 0
        self._prev_close: float | None = None
        self.last_state: MarketState | None = None

    def apply_runtime_config(self) -> None:
        """Apply hot-reloadable thresholds without discarding rolling state."""
        self._events.cfg = EventConfig(**self.cfg.event.model_dump())
        self._regime.threshold = self.cfg.composite.cascade_threshold
        self._regime.post_window_ms = self.cfg.composite.post_cascade_min * 60_000
        self._regime.adx_strong = self.cfg.indicators.adx_strong

    def reset_transient_event(self) -> None:
        """Do not turn a historical warm-up event into a new live order."""
        self._events = EventTracker(EventConfig(**self.cfg.event.model_dump()))
        if self.last_state is not None:
            self.last_state.event_active = False
            self.last_state.event_verdict = "none"

    # ------------------------------------------------------------ event inputs

    def on_force_order(self, fo: ForceOrder) -> None:
        self._liq_win.update(fo.ts, fo.notional)
        self._liq_24h.update(fo.ts, fo.notional)

    def on_mark_price(self, mp: MarkPrice) -> None:
        self._last_mark = mp
        self._funding_pred = mp.funding_rate

    @property
    def current_mark(self) -> float | None:
        return self._last_mark.mark if self._last_mark is not None else None

    def on_funding_settled(self, fr: FundingRate) -> None:
        self._funding_30d.update(fr.rate)
        self._funding_90d.update(fr.rate)
        if self._funding_pred is None:
            self._funding_pred = fr.rate

    def on_oi(self, oi: OISnapshot) -> None:
        self._oi.append(oi)
        self._oi_pctl_win.update(oi.oi)

    def on_fng(self, value: float) -> None:
        self._fng = value

    def on_lsr(self, value: float) -> None:
        self._lsr_last = value
        self._lsr_win.update(value)

    def on_xprice(self, venue: str, ts: int, price: float) -> None:
        """Reference price from another venue (bybit/okx/...) for cross-venue deviation."""
        self._xprices[venue] = (ts, price)

    def on_taker(self, ts: int, buy_qty: float, sell_qty: float) -> None:
        """Aggressor flow from the trade stream (aggTrade / publicTrade)."""
        self._taker_buy.update(ts, buy_qty)
        self._taker_sell.update(ts, sell_qty)

    def on_book(self, bt: BookTicker) -> None:
        self._last_book = bt

    def preload_funding(self, rates: list[FundingRate]) -> None:
        for fr in rates:
            self.on_funding_settled(fr)

    # ------------------------------------------------------------ candle close

    def on_candle(self, c: Candle) -> MarketState:
        self._candles_seen += 1
        ts = c.close_ts
        # returns & vol
        r = 0.0
        if self._prev_close is not None and self._prev_close > 0:
            r = math.log(c.close / self._prev_close)
            self._sigma.update(r)
            self._r2.update(r * r)
            self._ret_win.update(r)
        self._prev_close = c.close
        self._closes.append(c.close)
        ema = self._ema_anchor.update(c.close)
        atr = self._atr.update(c.high, c.low, c.close)
        self._vols.update(c.volume)
        self._liq_win.advance(ts)
        self._liq_24h.advance(ts)
        self._liq_minute.update(self._liq_win.sum())
        self._candles_since_liq_hist += 1
        if self._candles_since_liq_hist >= 60:
            self._liq_24h_hist.update(self._liq_24h.sum())
            self._candles_since_liq_hist = 0
        self._update_tf(c)
        self._update_h1(c)
        self._update_basis()

        st = MarketState(ts=ts, price=c.close)
        st.ema_anchor, st.atr = ema, atr
        st.sigma_1m = self._sigma.value
        st.vel1 = self._vel(1)
        st.vel3 = self._vel(3)
        st.vel5 = self._vel(5)
        if ema is not None and atr and atr > 0:
            st.dev = (c.close - ema) / atr
        st.atr_tf = self._atr_tf.value
        med = self._vols.median()
        if med and med > 0:
            st.vb = c.volume / med
        st.liq_count_60s = self._liq_win.count()
        st.liq_notional_60s = self._liq_win.sum()
        if len(self._liq_minute) >= 60:
            st.liq_pctl = self._liq_minute.percentile_rank(st.liq_notional_60s)
        st.doi_5m = self._doi(ts, 5 * 60_000)
        st.doi_1h = self._doi(ts, 60 * 60_000)
        st.open_interest = self._oi[-1].oi if self._oi else None
        st.funding_rate = self._funding_pred
        st.long_short_ratio = self._lsr_last
        if self._funding_pred is not None and len(self._funding_30d) >= 30:
            st.funding_pctl = self._funding_30d.percentile_rank(self._funding_pred)
        if len(self._r2) >= self.cfg.indicators.rv_window_min // 2:
            rv = math.sqrt(max(self._r2.sum(), 0.0))
            if len(self._rv_hist) >= 24 * 7:
                st.rv_pctl = self._rv_hist.percentile_rank(rv)
            if self._candles_seen % 60 == 0:
                self._rv_hist.update(rv)
        st.trend, st.adx = self._classify_trend()
        if len(self._basis_win) >= 200:
            mu, sd = self._basis_win.mean(), self._basis_win.std()
            if self._last_mark and sd and sd > 0 and self._last_mark.index > 0:
                st.basis_z = ((self._last_mark.mark - self._last_mark.index) / self._last_mark.index - mu) / sd
        st.fng = self._fng

        # ---- v0.2.0 fields (plan v2.1) ----
        st.ret_z_robust = self._robust_z(r) if self._candles_seen > 1 else None
        st.trend_score, st.trend_agree = self._trend_fields()
        st.donchian_break = self._donchian_break
        st.atr_1h = self._atr_1h.value
        # liquidation/OI ratio + intensity decay
        oi_notional = self._oi[-1].oi * c.close if self._oi else None
        if oi_notional and oi_notional > 0:
            st.liq_oi_ratio = st.liq_notional_60s / oi_notional
            self._liq_oi_minute.update(st.liq_oi_ratio)
            if len(self._liq_oi_minute) >= 120:
                st.liq_oi_pctl = self._liq_oi_minute.percentile_rank(st.liq_oi_ratio)
        self._liq_recent.append(st.liq_notional_60s)
        st.liq_decaying = self._liq_decay()
        if st.doi_5m is not None:
            self._doi_win.update(st.doi_5m)
            if len(self._doi_win) >= 200:
                st.doi_5m_pctl = self._doi_win.percentile_rank(st.doi_5m)
        st.xvenue_dev = self._xvenue_dev(c.close, ts, atr)
        st.taker_imbalance = self._taker_imb(ts)
        st.spread_pctl = self._spread_pctl(c)
        # extreme-event window + rejection/acceptance verdict
        ev = self._events.update(
            c, ret_z=st.ret_z_robust,
            liq_oi_pctl=st.liq_oi_pctl, doi_pctl=st.doi_5m_pctl,
            liq_notional_60s=st.liq_notional_60s, liq_decaying=st.liq_decaying,
            taker_imbalance=st.taker_imbalance, xvenue_dev=st.xvenue_dev)
        st.event_active = ev.active
        st.event_id = ev.event_id
        st.event_dir = ev.direction if ev.active else "none"
        st.event_extreme = ev.extreme if ev.active else None
        st.event_vwap = ev.vwap if ev.active else None
        st.reclaim_frac = self._events.reclaim_frac(c.close) if ev.active else None
        st.event_verdict = ev.verdict if ev.active else "none"

        # C layer
        cw = self.cfg.composite
        st.cascade_score, st.cascade_dir = composite.cascade_score(
            st.vel5, st.liq_pctl, st.vb, st.doi_5m, cw.cascade_weights)
        oi_pctl = self._oi_pctl_win.percentile_rank(self._oi[-1].oi) if self._oi and len(self._oi_pctl_win) >= 100 else None
        st.open_interest_pctl = oi_pctl
        lsr_z = None
        if self._lsr_last is not None and len(self._lsr_win) >= 200:
            mu, sd = self._lsr_win.mean(), self._lsr_win.std()
            if sd and sd > 0:
                lsr_z = (self._lsr_last - mu) / sd
        st.crowding_score = composite.crowding_score(st.funding_pctl, oi_pctl, lsr_z, st.basis_z, cw.crowding_weights)
        liq24_pctl = self._liq_24h_hist.percentile_rank(self._liq_24h.sum()) if len(self._liq_24h_hist) >= 48 else None
        st.eatfear, st.eatgreed = composite.eatfear_scores(
            st.fng, st.funding_pctl, st.cascade_score, st.cascade_dir, liq24_pctl, cw.eatfear_weights)
        st.regime = self._regime.update(ts, st.cascade_score, st.trend, st.rv_pctl)
        st.warmed_up = (self._candles_seen >= max(300, self.cfg.indicators.vb_window + 10)
                        and atr is not None and st.sigma_1m is not None and st.dev is not None)
        self.last_state = st
        return st

    # ------------------------------------------------------------ helpers

    def _vel(self, k: int) -> float | None:
        sig = self._sigma.value
        if sig is None or sig <= 0 or len(self._closes) <= k:
            return None
        now, then = self._closes[-1], self._closes[-1 - k]
        if then <= 0:
            return None
        return math.log(now / then) / (sig * math.sqrt(k))

    def _doi(self, ts: int, span_ms: int) -> float | None:
        if len(self._oi) < 2:
            return None
        latest = self._oi[-1]
        base = None
        for snap in reversed(self._oi):
            if ts - snap.ts >= span_ms:
                base = snap
                break
        if base is None or base.oi <= 0:
            return None
        return latest.oi / base.oi - 1.0

    def _update_tf(self, c: Candle) -> None:
        bucket = c.ts // self._tf_ms
        if self._tf_bucket is None:
            self._tf_bucket = bucket
            self._tf_candle = Candle(bucket * self._tf_ms, c.open, c.high, c.low, c.close, c.volume)
            return
        if bucket == self._tf_bucket:
            tc = self._tf_candle
            tc.high = max(tc.high, c.high)
            tc.low = min(tc.low, c.low)
            tc.close = c.close
            tc.volume += c.volume
            return
        # bucket rolled: finalize previous HTF candle
        done = self._tf_candle
        self._tf_count += 1
        e50 = self._ema50.update(done.close)
        self._ema200.update(done.close)
        self._ema50_hist.append(e50)
        self._adx.update(done)
        self._atr_tf.update(done.high, done.low, done.close)
        self._tf_bucket = bucket
        self._tf_candle = Candle(bucket * self._tf_ms, c.open, c.high, c.low, c.close, c.volume)

    def _classify_trend(self) -> tuple[Trend, float | None]:
        adx = self._adx.value
        e50, e200 = self._ema50.value, self._ema200.value
        if e50 is None or e200 is None or self._tf_count < 210:
            return Trend.NEUTRAL, adx
        up = e50 > e200
        slope_up = len(self._ema50_hist) >= 6 and self._ema50_hist[-1] > self._ema50_hist[0]
        strong = adx is not None and adx >= self.cfg.indicators.adx_strong
        if up and slope_up:
            return (Trend.STRONG_UP if strong else Trend.WEAK_UP), adx
        if not up and not slope_up:
            return (Trend.STRONG_DOWN if strong else Trend.WEAK_DOWN), adx
        return Trend.NEUTRAL, adx

    def _update_basis(self) -> None:
        if self._last_mark and self._last_mark.index > 0 and self._candles_seen % 5 == 0:
            self._basis_win.update((self._last_mark.mark - self._last_mark.index) / self._last_mark.index)

    # ------------------------------------------------------------ v0.2.0 helpers

    def _update_h1(self, c: Candle) -> None:
        bucket = c.ts // self._h1_ms
        if self._h1_bucket is None:
            self._h1_bucket = bucket
            self._h1_candle = Candle(bucket * self._h1_ms, c.open, c.high, c.low, c.close, c.volume)
            return
        if bucket == self._h1_bucket:
            hc = self._h1_candle
            hc.high = max(hc.high, c.high)
            hc.low = min(hc.low, c.low)
            hc.close = c.close
            hc.volume += c.volume
            return
        done = self._h1_candle
        # Donchian(20): the just-completed 1h close vs the PRIOR 20 bars' extremes
        if len(self._h1) >= 20:
            prior = list(self._h1)[-20:]
            hi = max(p.high for p in prior)
            lo = min(p.low for p in prior)
            self._donchian_break = 1 if done.close > hi else (-1 if done.close < lo else 0)
        if self._h1:
            prev_close = self._h1[-1].close
            if prev_close > 0 and done.close > 0:
                self._h1_ret_win.update(math.log(done.close / prev_close))
        self._h1.append(done)
        self._atr_1h.update(done.high, done.low, done.close)
        self._h1_bucket = bucket
        self._h1_candle = Candle(bucket * self._h1_ms, c.open, c.high, c.low, c.close, c.volume)

    def _trend_fields(self) -> tuple[float | None, int]:
        """Slow multi-horizon momentum: equal-weight 72h/7d/14d normalized returns."""
        sig_h = self._h1_ret_win.std()
        closes = [h.close for h in self._h1]
        if sig_h is None or sig_h <= 0 or len(closes) < 337:
            return None, 0
        now = closes[-1]
        zs = []
        for hours, w in ((72, 1 / 3), (168, 1 / 3), (336, 1 / 3)):
            then = closes[-1 - hours]
            if then <= 0:
                return None, 0
            zs.append((math.log(now / then) / (sig_h * math.sqrt(hours)), w))
        t = sum(z * w for z, w in zs)
        agree = sum(1 for z, _ in zs if (z > 0) == (t > 0) and abs(z) > 1e-12)
        return t, agree

    def _robust_z(self, r: float) -> float | None:
        if len(self._ret_win) < 300:
            return None
        med = self._ret_win.median()
        q75, q25 = self._ret_win.quantile(0.75), self._ret_win.quantile(0.25)
        if med is None or q75 is None or q25 is None:
            return None
        sigma_rb = (q75 - q25) / 1.349
        if sigma_rb <= 0:
            return None
        return (r - med) / sigma_rb

    def _liq_decay(self) -> bool | None:
        w = list(self._liq_recent)
        if len(w) < 5:
            return None
        peak = max(w)
        if peak <= 0:
            return None
        peak_idx = w.index(peak)
        return peak_idx <= len(w) - 3 and w[-1] < 0.6 * peak and w[-2] < 0.8 * peak

    def _xvenue_dev(self, local_price: float, ts: int, atr: float | None) -> float | None:
        """(local - median of other venues) / ATR. Stale quotes (>30s) are ignored."""
        if atr is None or atr <= 0:
            return None
        fresh = [p for v, (t, p) in self._xprices.items() if ts - t <= 30_000]
        if not fresh:
            return None
        fresh.sort()
        n = len(fresh)
        med = fresh[n // 2] if n % 2 else 0.5 * (fresh[n // 2 - 1] + fresh[n // 2])
        return (local_price - med) / atr

    def _taker_imb(self, ts: int) -> float | None:
        self._taker_buy.advance(ts)
        self._taker_sell.advance(ts)
        b, s = self._taker_buy.sum(), self._taker_sell.sum()
        total = b + s
        if total <= 0:
            return None
        return (b - s) / total

    def _spread_pctl(self, c: Candle) -> float | None:
        if self._last_book is None or self._last_book.mid <= 0:
            return None
        spread_bps = (self._last_book.ask - self._last_book.bid) / self._last_book.mid * 10_000
        self._spread_win.update(spread_bps)
        if len(self._spread_win) < 200:
            return None
        return self._spread_win.percentile_rank(spread_bps)

