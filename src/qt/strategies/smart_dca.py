"""Solution A — Smart DCA (volatility-aware dollar-cost averaging).

WHY THIS STRATEGY EXISTS
------------------------
Plain calendar DCA (fixed $X every week) is the most studied retail crypto
strategy. Two consistent findings in the literature:

1. **DCA dominates lump-sum on a Sharpe basis for highly volatile assets**
   even though it has lower expected return (Bouri & Roubaud 2017+).
2. **Cyclically-weighted DCA** that buys more during fear and less during
   greed historically beats flat DCA on both CAGR and max drawdown
   (Willy Woo's "Bitcoin Investor Tool" 2019; LookIntoBitcoin's MVRV
   weighted-DCA backtests; Glassnode 2022 research notes).

This strategy modulates the weekly buy amount by a composite "stress
score" assembled from publicly-available retail-friendly indicators:
Fear & Greed (alternative.me), MVRV-Z (Coin Metrics free tier or
Glassnode community), drawdown from 1-year high, and price vs 200-day
moving average. Everything except MVRV-Z is computable from OHLCV alone.

Buy multiplier: 1.0 + ``k * stress_score`` clipped to [0.25, 3.0].
``stress_score = +1`` = peak fear → buy 3× the base.
``stress_score = -1`` = peak greed → buy 0.25× the base (or skip).

PARAMETERS
----------
- ``base_buy_quote``:   base USDT per scheduled buy.
- ``buy_dow``:          day-of-week for the recurring buy (default: Monday).
- ``buy_hour_utc``:     hour-of-day UTC for the buy (default: 14:00 UTC,
                        post-US close, low spread on retail venues).
- ``multiplier_k``:     sensitivity of the multiplier to stress (default 2.0).
- ``mult_clip``:        ``(min, max)`` clip for the multiplier.
- ``take_profit_nupl``: if NUPL > this threshold and Pi Cycle Top fires,
                        trim N% of position to lock gains. Default disabled.
- ``tp_trim_frac``:     fraction of position to trim when take-profit fires.

KNOWN FAILURE MODES
-------------------
- **Look-ahead via MVRV/NUPL**: their underlying data publishes with a
  few-hour lag; we always shift by 1 bar before using.
- **Regime change**: BTC has fundamentally changed structure (ETF era,
  institutional flows). The "fear = buy" reflex needs MVRV/NUPL to
  anchor, since F&G can stay sub-20 for months in a true bear market.
- **Tax**: jurisdictions treat each buy as a new tax lot. The take-profit
  branch will realize gains; if you don't want that, set
  ``take_profit_nupl=None``.

REFERENCES
----------
- Willy Woo, *"Bitcoin Investor Tool"*, 2019, charts.woobull.com.
- LookIntoBitcoin / Philip Swift, MVRV-weighted DCA notebook (2020).
- Bouri, Molnár, Azzi, Roubaud, Hagfors (2017+) papers on BTC volatility
  and the DCA/lump-sum trade-off.
- Glassnode Insights, "DCA Strategies for the BTC Cycle" (2022).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qt.indicators.price import drawdown_from_high
from qt.strategies.base import StrategyResult


@dataclass
class SmartDCAConfig:
    base_buy_quote: float = 100.0
    buy_dow: int = 0                     # Monday
    buy_hour_utc: int = 14
    multiplier_k: float = 2.0
    mult_clip: tuple[float, float] = (0.25, 3.0)
    take_profit_nupl: float | None = None
    tp_trim_frac: float = 0.20
    fee_bps: float = 7.5
    slippage_bps: float = 5.0
    initial_cash: float = 10_000.0


def _stress_score(
    close: pd.Series,
    fear_greed: pd.Series | None,
    mvrv_z: pd.Series | None,
) -> pd.Series:
    """Combine 4 normalized signals into a single ``[-1, +1]`` stress score
    where +1 = "maximum fear, accumulate aggressively".

    Components (each clipped to [-1, +1] and then averaged with equal
    weight over the components we actually have data for):

    - ``F&G``:    +1 when F&G ≤ 10; -1 when F&G ≥ 90; linear in between.
    - ``MVRV-Z``: +1 when MVRV-Z ≤ 0; -1 when MVRV-Z ≥ 7; linear.
    - ``DD₁y``:   +1 when 1-year drawdown ≥ 60%; -1 when at ATH.
    - ``MA200``:  +1 when price 30% below 200d MA; -1 when 50% above.
    """

    parts: list[pd.Series] = []
    if fear_greed is not None and not fear_greed.empty:
        fg = fear_greed.reindex(close.index).ffill().shift(1)
        s_fg = ((50.0 - fg) / 40.0).clip(-1, 1)
        parts.append(s_fg)
    if mvrv_z is not None and not mvrv_z.empty:
        mv = mvrv_z.reindex(close.index).ffill().shift(1)
        s_mv = ((3.5 - mv) / 3.5).clip(-1, 1)
        parts.append(s_mv)
    dd = drawdown_from_high(close, window=365 * 24)  # 1-year DD on hourly bars
    s_dd = (-dd / 0.60).clip(-1, 1)
    parts.append(s_dd)
    ma200 = close.rolling(200 * 24).mean()
    rel = (close / ma200) - 1.0
    s_ma = ((-rel) / 0.40).clip(-1, 1)
    parts.append(s_ma)
    stack = pd.concat(parts, axis=1)
    return stack.mean(axis=1).rename("stress")


class SmartDCA:
    """Volatility-aware periodic DCA accumulator. See module docstring."""

    def __init__(self, cfg: SmartDCAConfig | None = None) -> None:
        self.cfg = cfg or SmartDCAConfig()

    def run(
        self,
        ohlcv: pd.DataFrame,
        *,
        fear_greed: pd.Series | None = None,
        mvrv_z: pd.Series | None = None,
        nupl: pd.Series | None = None,
    ) -> StrategyResult:
        cfg = self.cfg
        close = ohlcv["close"].astype("float64")
        idx = close.index

        is_buy_bar = (
            (idx.dayofweek == cfg.buy_dow) & (idx.hour == cfg.buy_hour_utc)
        )
        buy_bars = pd.Series(is_buy_bar, index=idx)

        stress = _stress_score(close, fear_greed, mvrv_z)
        multiplier = (1.0 + cfg.multiplier_k * stress).clip(*cfg.mult_clip)

        # Optional take-profit: if NUPL > threshold AND Pi Cycle Top fires,
        # trim a fraction of position. We express this as a downward jolt to
        # the target-weight series at that bar (one-off).
        tp_bars = pd.Series(False, index=idx)
        if cfg.take_profit_nupl is not None and nupl is not None and not nupl.empty:
            nupl_aligned = nupl.reindex(idx).ffill().shift(1)
            pi_top = _pi_cycle_top(close)
            tp_bars = (nupl_aligned >= cfg.take_profit_nupl) & pi_top

        cash = cfg.initial_cash
        qty = 0.0
        equity_pts: list[float] = []
        trades: list[dict] = []
        for ts, px in close.items():
            px_f = float(px)
            if bool(buy_bars.loc[ts]):
                amt = cfg.base_buy_quote * float(multiplier.loc[ts])
                amt = min(amt, cash)
                if amt > 0:
                    fee = amt * (cfg.fee_bps + cfg.slippage_bps) / 10_000.0
                    bought = (amt - fee) / px_f
                    qty += bought
                    cash -= amt
                    trades.append(
                        {
                            "ts": ts, "side": "buy", "qty": bought,
                            "price": px_f, "fee": fee, "leg": "spot",
                            "note": f"dca_mult_{float(multiplier.loc[ts]):.2f}",
                        }
                    )
            if bool(tp_bars.loc[ts]) and qty > 0:
                trim_qty = qty * cfg.tp_trim_frac
                proceeds = trim_qty * px_f
                fee = proceeds * (cfg.fee_bps + cfg.slippage_bps) / 10_000.0
                cash += proceeds - fee
                qty -= trim_qty
                trades.append(
                    {
                        "ts": ts, "side": "sell", "qty": -trim_qty,
                        "price": px_f, "fee": fee, "leg": "spot",
                        "note": "take_profit_pi_top",
                    }
                )
            equity_pts.append(cash + qty * px_f)

        equity = pd.Series(equity_pts, index=idx, name="equity")
        target_weight = (qty * close) / equity.replace(0, np.nan)
        target_weight = target_weight.fillna(0.0).clip(lower=0.0)
        diag = pd.DataFrame(
            {"stress": stress, "multiplier": multiplier, "buy_bar": buy_bars,
             "tp_bar": tp_bars}
        )
        return StrategyResult(
            equity=equity,
            target_weight=target_weight,
            short_weight=pd.Series(0.0, index=idx),
            trades=pd.DataFrame(trades),
            diagnostics=diag,
        )


def _pi_cycle_top(close: pd.Series) -> pd.Series:
    """Pi Cycle Top: 111d MA × 2 crosses *above* 350d MA.

    Mirror of the bottom indicator in qt.indicators.onchain. Lives here
    to keep the strategy self-contained (no extra public API needed).
    """

    bars_per_day = 24
    ma_fast = close.rolling(111 * bars_per_day).mean() * 2
    ma_slow = close.rolling(350 * bars_per_day).mean()
    cross = (ma_fast > ma_slow) & (ma_fast.shift(1) <= ma_slow.shift(1))
    return cross.fillna(False)


__all__ = ["SmartDCA", "SmartDCAConfig"]
