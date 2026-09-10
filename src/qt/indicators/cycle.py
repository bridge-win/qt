"""Four-year cycle positioning (super-long horizon) - data-calibrated.

Produces a 0-100 "cycle position" from several slow valuation gauges. It is
*not* a trade trigger; it is a target-allocation regime that the shorter
horizons operate inside of.

Band edges (what counts as "top" / "bottom" for each gauge) are **derived
from the public data at run time** by `calibrate_bands()`:

1. Cycle tops/bottoms are located from the price series using the halving
   clock (top = max within 2y after each halving, bottom = min between that
   top and the next halving).  Source: Bitstamp / Coin Metrics price.
2. For each gauge we record its value at those dates ("observed").
3. Peaks have declined every cycle (MVRV peak 5.7 -> 3.9 -> 3.7 -> 2.5;
   Mayer >3 -> 3.4 -> 2.0 -> 1.18), so the *current-cycle* top edge is a
   log-linear projection of the observed peaks one cycle ahead ("fitted",
   OLS, n = completed cycles).  Bottom edges use the mean of observed
   bottoms (they have been stable near the literature values).
4. If fewer than 3 completed cycles are available in the data, the
   literature thresholds from `qt.core.provenance` are used and labelled
   as such.

Every number in the output carries its method + source so the UI can show
"top edge 2.1 (fitted: OLS on 4 observed peaks from Coin Metrics)".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from qt.core import provenance as prov
from qt.indicators.onchain import (
    mayer_multiple,
    pi_cycle_bottom,
    pi_cycle_top,
    sma_200w_multiple,
)

HALVINGS = pd.to_datetime(prov.get("cycle.halvings").value, utc=True)
HALVING_PERIOD_DAYS = int(prov.get("cycle.period_days").value)
TOP_PHASE = float(prov.get("cycle.top_phase").value)

# literature fallbacks (lo = bottom edge, hi = top edge)
LITERATURE_BANDS: dict[str, tuple[float, float, tuple[str, str]]] = {
    "mvrv_z": (0.0, 7.0, ("mvrv_z.bottom_literature", "mvrv_z.top_literature")),
    "nupl": (0.0, 0.75, ("nupl.capitulation", "nupl.euphoria")),
    "mayer": (0.8, 2.4, ("mayer.bottom", "mayer.top")),
    "sma200w": (1.0, 4.0, ("sma200w.floor", "sma200w.top_literature")),
}


# ---------------------------------------------------------------------------
# Cycle extrema from price
# ---------------------------------------------------------------------------

@dataclass
class CycleExtremum:
    kind: str                # "top" | "bottom"
    cycle: int               # index of the halving epoch (0 = 2012 halving)
    date: pd.Timestamp
    price: float
    days_after_halving: int


def locate_cycle_extrema(daily_close: pd.Series) -> list[CycleExtremum]:
    """Locate tops/bottoms with the halving clock. Pure function of price."""

    close = daily_close.dropna()
    out: list[CycleExtremum] = []
    for i, h in enumerate(HALVINGS):
        nxt = HALVINGS[i + 1] if i + 1 < len(HALVINGS) else close.index[-1] + pd.Timedelta(days=1)
        win = close[(close.index >= h) & (close.index < min(nxt, h + pd.Timedelta(days=730)))]
        if win.empty or len(win) < 300:
            continue
        t_date = win.idxmax()
        # a top is only "completed" if the series continues >= 120 days past it
        if (close.index[-1] - t_date).days < 120:
            continue
        out.append(CycleExtremum("top", i, t_date, float(win.max()), (t_date - h).days))
        post = close[(close.index > t_date) & (close.index < nxt)]
        if len(post) >= 300 and (close.index[-1] - post.idxmin()).days >= 120:
            b_date = post.idxmin()
            out.append(CycleExtremum("bottom", i, b_date, float(post.min()), (b_date - h).days))
    return out


# ---------------------------------------------------------------------------
# Band calibration
# ---------------------------------------------------------------------------

@dataclass
class Band:
    gauge: str
    lo: float
    hi: float
    lo_method: str
    hi_method: str
    observed_tops: dict[str, float] = field(default_factory=dict)      # date -> value
    observed_bottoms: dict[str, float] = field(default_factory=dict)
    fit: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gauge": self.gauge, "lo": round(self.lo, 4), "hi": round(self.hi, 4),
            "lo_method": self.lo_method, "hi_method": self.hi_method,
            "observed_tops": self.observed_tops, "observed_bottoms": self.observed_bottoms,
            "fit": self.fit, "sources": self.sources,
        }


def _project_next_peak(peaks: list[float]) -> tuple[float, dict[str, Any]]:
    """Log-linear OLS of ln(peak) on cycle index, projected one step ahead."""

    y = np.log(np.asarray(peaks, dtype=float))
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (intercept + slope * x)
    proj = float(np.exp(intercept + slope * len(y)))
    return proj, {
        "estimator": "OLS ln(peak) ~ cycle_index", "n": len(y),
        "slope_per_cycle": round(float(slope), 4),
        "resid_std": round(float(resid.std(ddof=1)) if len(y) > 2 else float("nan"), 4),
        "points": [round(float(p), 4) for p in peaks],
    }


def calibrate_bands(
    daily_close: pd.Series,
    gauges: dict[str, pd.Series],
    min_cycles: int = 3,
) -> dict[str, Band]:
    """Derive per-gauge (bottom, top) edges from observed cycle extrema.

    ``gauges`` maps gauge name -> daily series (already computed from public
    data). Missing cycles fall back to literature values, labelled.
    """

    ext = locate_cycle_extrema(daily_close)
    tops = [e for e in ext if e.kind == "top"]
    bottoms = [e for e in ext if e.kind == "bottom"]
    src_names = [prov.SOURCES["bitstamp"].name, prov.SOURCES["coinmetrics"].name]
    bands: dict[str, Band] = {}

    for name, series in gauges.items():
        s = series.dropna()
        lit_lo, lit_hi, lit_keys = LITERATURE_BANDS.get(name, (0.0, 1.0, ("", "")))
        obs_t = {e.date.strftime("%Y-%m-%d"): float(s.reindex([e.date], method="nearest").iloc[0])
                 for e in tops if not s.empty and abs((s.index[np.argmin(np.abs(s.index - e.date))] - e.date).days) <= 3}
        obs_b = {e.date.strftime("%Y-%m-%d"): float(s.reindex([e.date], method="nearest").iloc[0])
                 for e in bottoms if not s.empty and abs((s.index[np.argmin(np.abs(s.index - e.date))] - e.date).days) <= 3}
        band = Band(gauge=name, lo=lit_lo, hi=lit_hi,
                    lo_method=f"literature ({lit_keys[0]})", hi_method=f"literature ({lit_keys[1]})",
                    observed_tops=obs_t, observed_bottoms=obs_b, sources=src_names)
        peak_vals = [v for v in obs_t.values() if np.isfinite(v) and v > 0]
        if len(peak_vals) >= min_cycles:
            proj, fit = _project_next_peak(peak_vals)
            # never project below the last observed peak's 0.6x (guards the decay fit
            # from overshooting on a short sample)
            floor = 0.6 * peak_vals[-1]
            band.hi = max(proj, floor)
            band.hi_method = ("fitted (" + prov.get("cycle.decay_fit").value + ")"
                              + (" - floored at 0.6x last peak" if proj < floor else ""))
            band.fit = fit
        elif peak_vals:
            band.hi = float(np.mean(peak_vals))
            band.hi_method = f"observed mean of {len(peak_vals)} cycle top(s); <{min_cycles} cycles so no decay fit"
        bot_vals = [v for v in obs_b.values() if np.isfinite(v)]
        if bot_vals:
            band.lo = float(np.mean(bot_vals))
            band.lo_method = f"observed mean of {len(bot_vals)} cycle bottom(s)"
        if band.hi <= band.lo:
            band.hi, band.hi_method = lit_hi, f"literature ({lit_keys[1]}) - fitted edge collapsed onto bottom"
        bands[name] = band
    return bands


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

def _band(x: pd.Series, lo: float, hi: float) -> pd.Series:
    return ((x - lo) / (hi - lo)).clip(0.0, 1.0)


def halving_phase(index: pd.DatetimeIndex) -> pd.Series:
    idx = pd.DatetimeIndex(index)
    out = pd.Series(np.nan, index=idx, dtype="float64")
    for i, ts in enumerate(idx):
        past = HALVINGS[ts >= HALVINGS]
        if len(past) == 0:
            continue
        out.iloc[i] = ((ts - past[-1]).days % HALVING_PERIOD_DAYS) / HALVING_PERIOD_DAYS
    return out.rename("halving_phase")


@dataclass
class CyclePosition:
    position: pd.Series
    components: pd.DataFrame
    band: pd.Series
    target_alloc: pd.Series
    events: pd.DataFrame
    bands: dict[str, Band]
    provenance: dict[str, Any]
    extrema: list[CycleExtremum]
    components_raw: pd.DataFrame = field(default_factory=pd.DataFrame)


def cycle_position(
    daily_close: pd.Series,
    mvrv_z: pd.Series | None = None,
    nupl: pd.Series | None = None,
    weights: dict[str, float] | None = None,
    price_source: str = "bitstamp",
    onchain_source: str = "coinmetrics",
) -> CyclePosition:
    close = daily_close.astype("float64")
    idx = close.index

    gauges: dict[str, pd.Series] = {"mayer": mayer_multiple(close)}
    w200 = sma_200w_multiple(close)
    if w200.notna().any():
        gauges["sma200w"] = w200
    if mvrv_z is not None and not mvrv_z.empty:
        gauges["mvrv_z"] = mvrv_z.reindex(idx).ffill()
    if nupl is not None and not nupl.empty:
        gauges["nupl"] = nupl.reindex(idx).ffill()

    bands = calibrate_bands(close, gauges)
    comps = {k: _band(v, bands[k].lo, bands[k].hi) for k, v in gauges.items()}
    hp = halving_phase(idx)
    comps["halving"] = (1.0 - (hp - TOP_PHASE).abs() / (1.0 - TOP_PHASE)).clip(0, 1)

    default_w: dict[str, float] = dict(prov.get("cycle.weights").value)
    w = {k: (weights or default_w).get(k, default_w.get(k, 1.0)) for k in comps}
    comp_df = pd.DataFrame(comps)
    wsum = comp_df.notna().mul(pd.Series(w)).sum(axis=1)
    pos = (comp_df.mul(pd.Series(w)).sum(axis=1, min_count=1) / wsum.replace(0, np.nan)) * 100.0
    pos = pos.rename("cycle_position")

    band = pd.cut(pos, bins=[-1, 15, 35, 65, 85, 101],
                  labels=["accumulate", "hold", "neutral", "trim", "distribute"]).astype("object").rename("band")
    curve = {int(k): v for k, v in prov.get("cycle.alloc_curve").value.items()}
    xs, ys = zip(*sorted(curve.items()), strict=True)
    target = pd.Series(np.interp(pos.fillna(50).to_numpy(), xs, ys), index=idx, name="target_alloc")

    events = pd.DataFrame({
        "pi_cycle_top": pi_cycle_top(close),
        "pi_cycle_bottom": pi_cycle_bottom(close) if len(close) > 471 else pd.Series(False, index=idx),
    }).fillna(False).astype(bool)

    extrema = locate_cycle_extrema(close)
    provenance = {
        "price": prov.series_provenance(price_source),
        "onchain": prov.series_provenance(onchain_source) if (mvrv_z is not None or nupl is not None) else None,
        "bands": {k: b.as_dict() for k, b in bands.items()},
        "weights": prov.annotate(w, "cycle.weights"),
        "halving_top_phase": prov.annotate(TOP_PHASE, "cycle.top_phase"),
        "alloc_curve": prov.annotate(curve, "cycle.alloc_curve"),
        "cycle_extrema": [
            {"kind": e.kind, "cycle": e.cycle, "date": e.date.strftime("%Y-%m-%d"),
             "price": round(e.price, 2), "days_after_halving": e.days_after_halving,
             "method": "observed (halving-clock window max/min on price series)"}
            for e in extrema
        ],
    }
    return CyclePosition(position=pos, components=comp_df, band=band, target_alloc=target,
                         events=events, bands=bands, provenance=provenance, extrema=extrema,
                         components_raw=pd.DataFrame(gauges))


__all__ = [
    "HALVINGS",
    "LITERATURE_BANDS",
    "Band",
    "CycleExtremum",
    "CyclePosition",
    "calibrate_bands",
    "cycle_position",
    "halving_phase",
    "locate_cycle_extrema",
]
