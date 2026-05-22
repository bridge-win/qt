"""Solution B — Multi-factor capitulation buyer (extreme-event mean reversion).

WHY THIS STRATEGY EXISTS
------------------------
The most studied "edge" for retail crypto is **mean reversion after
extreme capitulation**:

- Caporale et al. (2018) found BTC exhibits regime-dependent
  mean-reversion at the daily horizon, strongest after −3σ moves.
- Gkillas & Katsiampa (2018) showed that BTC daily returns past the
  99th-percentile loss are followed by significantly positive 1–5 day
  returns (extreme value theory).
- Glassnode and CryptoQuant practitioners (Checkmate, Ki Young Ju)
  consistently identify "capitulation windows" — multiple on-chain
  indicators below their historical extremes simultaneously — as the
  highest expected-value entries 2017–2023.

This strategy is a thin orchestration on top of the existing
``compute_extreme_score`` composite (price / vol / derivatives / on-chain
/ sentiment / smart-money / events). It adds three things the raw
composite does not provide:

1. **Tranche entry**: instead of going to a full target on the first
   firing bar (often a "false bottom"), build the position in 2–3
   tranches over up to N bars, only adding when the next tranche's
   trigger is also satisfied.
2. **Multi-condition exit** (any of):
   - MVRV-Z mean-reverts above ``exit_mvrv_z``  (~2.0 historically).
   - Trailing ATR×k stop (``trail_atr_k`` × ATR(14)).
   - Time stop after ``max_holding_bars``.
3. **Cooldown**: after closing a trade, suppress new entries for
   ``cooldown_bars`` to avoid round-tripping the same regime.

KNOWN FAILURE MODES
-------------------
- **Very low sample size**: 5–10 firings per decade. Statistical
  confidence is limited; backtest results are *necessary but not
  sufficient*.
- **Continued downside**: tranches limit damage, but FTX-style cascades
  can keep falling for weeks; the ATR trailing stop must be loose
  enough to survive normal noise. Default 3× ATR(14) is a compromise.
- **Macro shocks**: in 2022 the macro veto (VIX < 35, DXY 20d Z < 2)
  prevented entry until late June, but the bottom was actually in
  November. Treat the veto as advisory, not authoritative.

REFERENCES
----------
- Caporale, Gil-Alana, Plastun (2018), *Persistence in the
  Cryptocurrency Market*.
- Gkillas & Katsiampa (2018), *An Application of Extreme Value Theory
  to Cryptocurrencies*.
- Glassnode Insights, "On-Chain Capitulation Models" (2022).
- Charles Edwards (Capriole), *Bitcoin Macro Index*.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from qt.core.config import ThresholdConfig
from qt.indicators.composite import compute_extreme_score
from qt.indicators.price import atr
from qt.strategies.base import StrategyResult


@dataclass
class CapitulationConfig:
    score_min: float = 0.60
    min_groups_firing: int = 4
    tranches: int = 3                         # split entry into N pieces
    tranche_spacing_bars: int = 24            # min bars between tranches
    max_position_weight: float = 0.60         # cap full position at 60% equity
    trail_atr_k: float = 3.0
    exit_mvrv_z: float = 2.0                  # exit when MVRV-Z recovers
    max_holding_bars: int = 30 * 24 * 4       # ~4 months on 1h bars
    cooldown_bars: int = 14 * 24              # 2 weeks after exit
    fee_bps: float = 7.5
    slippage_bps: float = 5.0
    initial_cash: float = 10_000.0
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)


class Capitulation:
    """Multi-factor extreme-event mean-reversion buyer. See module docstring."""

    def __init__(self, cfg: CapitulationConfig | None = None) -> None:
        self.cfg = cfg or CapitulationConfig()

    def run(
        self,
        ohlcv: pd.DataFrame,
        *,
        funding: pd.Series | None = None,
        oi: pd.Series | None = None,
        sopr: pd.Series | None = None,
        mvrv_z: pd.Series | None = None,
        nupl: pd.Series | None = None,
        puell: pd.Series | None = None,
        reserve_risk: pd.Series | None = None,
        exchange_netflow: pd.Series | None = None,
        fear_greed: pd.Series | None = None,
        social_sentiment: pd.Series | None = None,
        vix: pd.Series | None = None,
        dxy: pd.Series | None = None,
    ) -> StrategyResult:
        cfg = self.cfg
        close = ohlcv["close"].astype("float64")
        high = ohlcv["high"].astype("float64")
        low = ohlcv["low"].astype("float64")
        idx = close.index

        es = compute_extreme_score(
            ohlcv,
            funding=funding, oi=oi, sopr=sopr, mvrv_z=mvrv_z, nupl=nupl,
            puell=puell, reserve_risk=reserve_risk,
            exchange_netflow=exchange_netflow, fear_greed=fear_greed,
            social_sentiment=social_sentiment, vix=vix, dxy=dxy,
            cfg=cfg.thresholds,
        )
        score = es.score.reindex(idx).fillna(0.0)
        groups_firing = es.group_flags.sum(axis=1).reindex(idx).fillna(0).astype(int)
        macro_ok = (
            es.macro_ok.reindex(idx).fillna(True)
            if es.macro_ok is not None else pd.Series(True, index=idx)
        )
        trigger = (
            (score >= cfg.score_min)
            & (groups_firing >= cfg.min_groups_firing)
            & macro_ok
        )

        atr14 = atr(high, low, close, 14)

        mvrv_aligned = (
            mvrv_z.reindex(idx).ffill() if mvrv_z is not None else
            pd.Series(np.nan, index=idx)
        )

        cash = cfg.initial_cash
        qty = 0.0
        equity_pts: list[float] = []
        trades: list[dict] = []
        tranches_filled = 0
        last_tranche_bar: pd.Timestamp | None = None
        peak_since_entry = 0.0
        cooldown_until: pd.Timestamp | None = None
        entry_ts: pd.Timestamp | None = None
        entry_idx: int = -1
        target_w_series: list[float] = []

        for i, ts in enumerate(idx):
            px = float(close.iloc[i])
            atr_v = float(atr14.iloc[i]) if pd.notna(atr14.iloc[i]) else px * 0.02
            equity = cash + qty * px

            # --- Exit checks --------------------------------------------------
            if qty > 0:
                peak_since_entry = max(peak_since_entry, px)
                trail_stop = peak_since_entry - cfg.trail_atr_k * atr_v
                hit_trail = px <= trail_stop
                hit_mvrv = (
                    pd.notna(mvrv_aligned.iloc[i])
                    and float(mvrv_aligned.iloc[i]) >= cfg.exit_mvrv_z
                )
                bars_held = i - entry_idx if entry_idx >= 0 else 0
                hit_time = bars_held >= cfg.max_holding_bars
                if hit_trail or hit_mvrv or hit_time:
                    proceeds = qty * px
                    fee = proceeds * (cfg.fee_bps + cfg.slippage_bps) / 10_000.0
                    cash += proceeds - fee
                    reason = (
                        "trail_stop" if hit_trail else
                        "mvrv_recovered" if hit_mvrv else "time_stop"
                    )
                    trades.append(
                        {"ts": ts, "side": "sell", "qty": -qty, "price": px,
                         "fee": fee, "leg": "spot", "note": reason}
                    )
                    qty = 0.0
                    tranches_filled = 0
                    peak_since_entry = 0.0
                    last_tranche_bar = None
                    entry_ts = None
                    entry_idx = -1
                    cooldown_until = ts + pd.Timedelta(hours=cfg.cooldown_bars)

            # --- Entry / tranche checks --------------------------------------
            in_cooldown = cooldown_until is not None and ts < cooldown_until
            should_add = (
                bool(trigger.iloc[i])
                and not in_cooldown
                and tranches_filled < cfg.tranches
                and (
                    last_tranche_bar is None
                    or (ts - last_tranche_bar) >= pd.Timedelta(
                        hours=cfg.tranche_spacing_bars
                    )
                )
            )
            if should_add:
                tranche_size_q = (cfg.max_position_weight / cfg.tranches) * equity
                tranche_size_q = min(tranche_size_q, cash)
                if tranche_size_q > 0:
                    fee = tranche_size_q * (cfg.fee_bps + cfg.slippage_bps) / 10_000.0
                    bought = (tranche_size_q - fee) / px
                    qty += bought
                    cash -= tranche_size_q
                    tranches_filled += 1
                    last_tranche_bar = ts
                    if entry_ts is None:
                        entry_ts = ts
                        entry_idx = i
                        peak_since_entry = px
                    trades.append(
                        {"ts": ts, "side": "buy", "qty": bought, "price": px,
                         "fee": fee, "leg": "spot",
                         "note": f"tranche_{tranches_filled}/{cfg.tranches}"}
                    )

            eq_now = cash + qty * px
            equity_pts.append(eq_now)
            target_w_series.append((qty * px) / eq_now if eq_now > 0 else 0.0)

        equity = pd.Series(equity_pts, index=idx, name="equity")
        target_weight = pd.Series(target_w_series, index=idx, name="target_weight")
        diag = pd.DataFrame(
            {"score": score, "groups_firing": groups_firing,
             "trigger": trigger, "macro_ok": macro_ok}
        )
        return StrategyResult(
            equity=equity,
            target_weight=target_weight,
            short_weight=pd.Series(0.0, index=idx),
            trades=pd.DataFrame(trades),
            diagnostics=diag,
        )


__all__ = ["Capitulation", "CapitulationConfig"]
