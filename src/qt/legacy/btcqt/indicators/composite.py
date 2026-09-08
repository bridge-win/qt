# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""C-layer composite indices (v2 plan §3.4).

All weights/thresholds are INITIAL values pending Phase-1 calibration.
Components are normalized to [0, ~1.5] then clipped so one extreme input
cannot single-handedly max the score; missing inputs renormalize weights
instead of silently reading as zero.
"""
from __future__ import annotations


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _weighted(parts: dict[str, tuple[float | None, float]]) -> float:
    """parts: name -> (component in [0,1.5] or None if unavailable, weight).
    Returns weighted mean over available components, renormalizing weights."""
    num = den = 0.0
    for comp, w in parts.values():
        if comp is None:
            continue
        num += w * _clip(comp, 0.0, 1.5)
        den += w
    return num / den if den > 0 else 0.0


def cascade_score(vel5: float | None, liq_pctl: float | None, vb: float | None,
                  doi_5m: float | None, weights: dict[str, float]) -> tuple[float, str]:
    """C1: 0-100 + direction. Extreme velocity/liquidations/volume/OI-collapse."""
    direction = "none"
    if vel5 is not None and abs(vel5) >= 1.0:
        direction = "down" if vel5 < 0 else "up"
    raw = _weighted({
        "vel": (None if vel5 is None else abs(vel5) / 6.0, weights["vel"]),
        "liq": (None if liq_pctl is None else max(0.0, (liq_pctl - 50.0) / 50.0), weights["liq"]),
        "vb": (None if vb is None else vb / 10.0, weights["vb"]),
        "doi": (None if doi_5m is None else max(0.0, -doi_5m / 0.02), weights["doi"]),
    })
    return 100.0 * _clip(raw, 0.0, 1.0), direction


def crowding_score(funding_pctl: float | None, oi_pctl: float | None,
                   lsr_z: float | None, basis_z: float | None,
                   weights: dict[str, float]) -> float:
    """C2: signed -100..+100. Positive = longs crowded, negative = shorts crowded."""
    if funding_pctl is None:
        return 0.0
    sign = 1.0 if funding_pctl >= 50.0 else -1.0
    fp_extreme = _clip((abs(funding_pctl - 50.0) - 30.0) / 20.0, 0.0, 1.0)  # kicks in beyond p80/p20
    magnitude = _weighted({
        "funding": (fp_extreme, weights["funding"]),
        "oi": (None if oi_pctl is None else max(0.0, (oi_pctl - 50.0) / 50.0), weights["oi"]),
        "lsr": (None if lsr_z is None else abs(lsr_z) / 3.0, weights["lsr"]),
        "basis": (None if basis_z is None else abs(basis_z) / 3.0, weights["basis"]),
    })
    return sign * 100.0 * _clip(magnitude, 0.0, 1.0)


def eatfear_scores(fng: float | None, funding_pctl: float | None,
                   cascade: float, cascade_dir: str, liq24h_pctl: float | None,
                   weights: dict[str, float]) -> tuple[float, float]:
    """C4: (eatfear, eatgreed), each 0-100.
    eatfear: how extreme is panic-driven forced selling right now.
    eatgreed: symmetric for euphoric squeezes."""
    fear = None if fng is None else (100.0 - fng) / 100.0
    greed = None if fng is None else fng / 100.0
    neg_funding = None if funding_pctl is None else _clip((50.0 - funding_pctl - 30.0) / 20.0, 0.0, 1.0)
    pos_funding = None if funding_pctl is None else _clip((funding_pctl - 50.0 - 30.0) / 20.0, 0.0, 1.0)
    liq = None if liq24h_pctl is None else liq24h_pctl / 100.0
    ef = _weighted({
        "fear": (fear, weights["fear"]),
        "neg_funding": (neg_funding, weights["neg_funding"]),
        "cascade_down": (cascade / 100.0 if cascade_dir == "down" else 0.0, weights["cascade_down"]),
        "liq24h": (liq, weights["liq24h"]),
    })
    eg = _weighted({
        "fear": (greed, weights["fear"]),
        "neg_funding": (pos_funding, weights["neg_funding"]),
        "cascade_down": (cascade / 100.0 if cascade_dir == "up" else 0.0, weights["cascade_down"]),
        "liq24h": (liq, weights["liq24h"]),
    })
    return 100.0 * _clip(ef, 0.0, 1.0), 100.0 * _clip(eg, 0.0, 1.0)


