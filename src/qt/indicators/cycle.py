"""Four-year cycle positioning (super-long horizon).

Produces a 0-100 "cycle position" from several independent, slow-moving
valuation gauges. It is *not* a trade trigger; it is a target-allocation
regime that the shorter horizons operate inside of.

Components (all daily):
- MVRV-Z            (free: computed from Coin Metrics caps)
- NUPL              (free: (MC-RC)/MC)
- Mayer Multiple    (close / SMA200D)
- 200-week multiple (close / SMA1400D)
- Pi Cycle Top/Bottom events (SMA crosses)
- Halving clock     (days since last halving, mod ~1460)

Each gauge is mapped to [0, 1] with historically observed band edges
(bottom → 0, top → 1) and the average is scaled to 0-100. Band edges are
deliberately fixed rather than fitted: the point of this layer is to be
boring and hard to over-fit.

Reference bands (2013-2025):
    MVRV-Z   bottoms ≈ -0.5..0.5,   tops ≈ 7..10
    NUPL     bottoms ≈ -0.2..0,     tops ≈ 0.7..0.75
    Mayer    bottoms ≈ 0.6..0.8,    tops ≈ 2.4..3.0
    200W     bottoms ≈ 0.9..1.0,    tops ≈ 3..5 (diminishing each cycle)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qt.indicators.onchain import (
    mayer_multiple,
    pi_cycle_bottom,
    pi_cycle_top,
    sma_200w_multiple,
)

HALVINGS = pd.to_datetime(
    ["2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20"], utc=True
)
HALVING_PERIOD_DAYS = 1460


def _band(x: pd.Series, lo: float, hi: float) -> pd.Series:
    """Linear map lo→0, hi→1, clipped."""

    return ((x - lo) / (hi - lo)).clip(0.0, 1.0)


def halving_phase(index: pd.DatetimeIndex) -> pd.Series:
    """Fraction of the way through the current halving cycle, 0..1.

    Historically the price top arrived ~0.35-0.40 of the way through and
    the bottom ~0.75-0.80. This is the weakest input and gets the lowest
    weight.
    """

    idx = pd.DatetimeIndex(index)
    out = pd.Series(np.nan, index=idx, dtype="float64")
    for i, ts in enumerate(idx):
        past = HALVINGS[ts >= HALVINGS]
        if len(past) == 0:
            continue
        days = (ts - past[-1]).days
        out.iloc[i] = (days % HALVING_PERIOD_DAYS) / HALVING_PERIOD_DAYS
    return out.rename("halving_phase")


@dataclass
class CyclePosition:
    position: pd.Series          # 0..100
    components: pd.DataFrame     # each gauge in 0..1
    band: pd.Series              # "accumulate" | "hold" | "neutral" | "trim" | "distribute"
    target_alloc: pd.Series      # suggested BTC allocation fraction 0..1
    events: pd.DataFrame         # pi_cycle_top / pi_cycle_bottom booleans


def cycle_position(
    daily_close: pd.Series,
    mvrv_z: pd.Series | None = None,
    nupl: pd.Series | None = None,
    weights: dict[str, float] | None = None,
) -> CyclePosition:
    """Compute the cycle position on a daily close series (UTC index)."""

    close = daily_close.astype("float64")
    idx = close.index
    comps: dict[str, pd.Series] = {}

    comps["mayer"] = _band(mayer_multiple(close), 0.7, 2.6)
    w200 = sma_200w_multiple(close)
    if w200.notna().any():
        comps["sma200w"] = _band(w200, 1.0, 4.0)
    if mvrv_z is not None and not mvrv_z.empty:
        comps["mvrv_z"] = _band(mvrv_z.reindex(idx).ffill(), 0.0, 7.5)
    if nupl is not None and not nupl.empty:
        comps["nupl"] = _band(nupl.reindex(idx).ffill(), -0.1, 0.72)
    comps["halving"] = _band(halving_phase(idx), 0.0, 1.0)
    # halving phase is not monotone with price; fold so that 0.4 ≈ top
    hp = halving_phase(idx)
    comps["halving"] = (1.0 - (hp - 0.38).abs() / 0.62).clip(0, 1)

    default_w = {"mvrv_z": 3.0, "nupl": 2.0, "mayer": 2.0, "sma200w": 2.0, "halving": 1.0}
    w = {k: (weights or default_w).get(k, default_w[k]) for k in comps}
    comp_df = pd.DataFrame(comps)
    wsum = comp_df.notna().mul(pd.Series(w)).sum(axis=1)
    pos = (comp_df.mul(pd.Series(w)).sum(axis=1, min_count=1) / wsum.replace(0, np.nan)) * 100.0
    pos = pos.rename("cycle_position")

    band = pd.cut(
        pos,
        bins=[-1, 15, 35, 65, 85, 101],
        labels=["accumulate", "hold", "neutral", "trim", "distribute"],
    ).astype("object").rename("band")

    # piecewise allocation: 100% at ≤15, 75% at 35, 50% at 65, 25% at 85, 10% at 100
    target = pd.Series(
        np.interp(pos.fillna(50).to_numpy(), [0, 15, 35, 65, 85, 100],
                  [1.0, 1.0, 0.75, 0.50, 0.25, 0.10]),
        index=idx, name="target_alloc",
    )

    events = pd.DataFrame({
        "pi_cycle_top": pi_cycle_top(close),
        "pi_cycle_bottom": pi_cycle_bottom(close) if len(close) > 471
        else pd.Series(False, index=idx),
    }).fillna(False).astype(bool)

    return CyclePosition(position=pos, components=comp_df, band=band,
                         target_alloc=target, events=events)


__all__ = ["HALVINGS", "CyclePosition", "cycle_position", "halving_phase"]
