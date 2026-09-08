# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Phase-1 calibration: does the phenomenon exist, and where are the thresholds?

Runs the indicator engine over history WITHOUT trading and measures:
  - wick events (|dev| beyond level, or cascade_score >= 70): how often does
    price revert toward the anchor within 15m / 1h / 4h, and by how much
  - funding extremes: forward returns after crowded-percentile readings
Outputs a JSON dict; the CLI renders it as a markdown report.
These numbers replace the config's initial thresholds before Phase 2.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..config import Config
from ..indicators.engine import IndicatorEngine
from ..models import Candle, FundingRate

log = logging.getLogger(__name__)

HORIZONS_MIN = (15, 60, 240)


def run_calibration(cfg: Config, klines: pd.DataFrame, funding: pd.DataFrame) -> dict:
    eng = IndicatorEngine(cfg)
    pre = funding[funding.ts < int(klines.ts.iloc[0])] if len(klines) else funding
    eng.preload_funding([FundingRate(int(r.ts), float(r.rate)) for r in pre.itertuples()])
    fund_iter = funding[funding.ts >= int(klines.ts.iloc[0])].itertuples() if len(klines) else iter(())
    next_fund = next(fund_iter, None)

    closes = klines.close.to_numpy()
    arr = klines[["ts", "open", "high", "low", "close", "volume"]].to_numpy()
    events_wick: list[dict] = []
    events_fund: list[dict] = []
    emas: list[float] = []

    for i, row in enumerate(arr):
        c = Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                   float(row[4]), float(row[5]))
        while next_fund is not None and next_fund.ts <= c.close_ts:
            eng.on_funding_settled(FundingRate(int(next_fund.ts), float(next_fund.rate)))
            next_fund = next(fund_iter, None)
        st = eng.on_candle(c)
        emas.append(st.ema_anchor if st.ema_anchor is not None else c.close)
        if not st.warmed_up:
            continue
        if st.dev is not None and abs(st.dev) >= 3.0:
            events_wick.append({"i": i, "ts": st.ts, "dev": st.dev, "price": c.close,
                                "ema": st.ema_anchor, "atr": st.atr,
                                "cascade": st.cascade_score, "vel5": st.vel5})
        if st.funding_pctl is not None and (st.funding_pctl >= 97.5 or st.funding_pctl <= 2.5):
            events_fund.append({"i": i, "ts": st.ts, "pctl": st.funding_pctl, "price": c.close})

    return {
        "n_candles": len(arr),
        "wick": _wick_stats(events_wick, closes, emas),
        "funding": _funding_stats(events_fund, closes),
        "notes": [
            "wick reversion = share of |dev|>=3 events where price moved back toward the "
            "anchor by >=40% of the deviation within the horizon (S1's TP definition)",
            "funding forward returns are signed AGAINST the crowd (positive = fading worked)",
            "liquidation-feed percentiles need recorded live data; this run uses the "
            "velocity/volume proxy only",
        ],
    }


def _dedupe(events: list[dict], min_gap: int = 30) -> list[dict]:
    out, last_i = [], -10**9
    for e in events:
        if e["i"] - last_i >= min_gap:
            out.append(e)
            last_i = e["i"]
    return out


def _wick_stats(events: list[dict], closes: np.ndarray, emas: list[float]) -> dict:
    events = _dedupe(events)
    if not events:
        return {"n_events": 0}
    out: dict = {"n_events": len(events)}
    for hz in HORIZONS_MIN:
        reverted, depths = [], []
        for e in events:
            j = e["i"] + hz
            if j >= len(closes):
                continue
            target_move = 0.40 * (e["ema"] - e["price"])          # S1 TP definition
            # did any close within horizon recover >= 40% toward the anchor?
            seg = closes[e["i"] + 1: j + 1]
            if e["dev"] < 0:
                reverted.append(bool((seg >= e["price"] + target_move).any()))
            else:
                reverted.append(bool((seg <= e["price"] + target_move).any()))
            depths.append((closes[j] - e["price"]) / e["price"] * (1 if e["dev"] < 0 else -1))
        if reverted:
            out[f"revert_rate_{hz}m"] = round(float(np.mean(reverted)), 3)
            out[f"fwd_ret_toward_anchor_{hz}m_bps"] = round(float(np.mean(depths)) * 10_000, 1)
    down = [e for e in events if e["dev"] < 0]
    out["n_down"] = len(down)
    out["n_up"] = len(events) - len(down)
    return out


def _funding_stats(events: list[dict], closes: np.ndarray) -> dict:
    events = _dedupe(events, min_gap=480)                          # one per 8h bucket
    if not events:
        return {"n_events": 0}
    out: dict = {"n_events": len(events)}
    for hz in (480, 1440, 4320):                                   # 8h, 1d, 3d
        rets = []
        for e in events:
            j = e["i"] + hz
            if j >= len(closes):
                continue
            r = closes[j] / e["price"] - 1
            rets.append(-r if e["pctl"] >= 50 else r)              # signed against the crowd
        if rets:
            out[f"fade_ret_{hz//60}h_bps"] = round(float(np.mean(rets)) * 10_000, 1)
            out[f"fade_win_{hz//60}h"] = round(float(np.mean([x > 0 for x in rets])), 3)
    return out


def format_calibration(c: dict) -> str:
    lines = ["=" * 56, "PHASE-1 CALIBRATION REPORT (phenomenon check)", "=" * 56,
             f"candles analysed: {c['n_candles']}"]
    w = c.get("wick", {})
    lines.append(f"\n[wick events |dev|>=3] n={w.get('n_events', 0)} "
                 f"(down {w.get('n_down', 0)} / up {w.get('n_up', 0)})")
    for k, v in w.items():
        if k.startswith(("revert_rate", "fwd_ret")):
            lines.append(f"  {k:34s}: {v}")
    f = c.get("funding", {})
    lines.append(f"\n[funding extremes p>=97.5 / p<=2.5] n={f.get('n_events', 0)}")
    for k, v in f.items():
        if k.startswith("fade"):
            lines.append(f"  {k:34s}: {v}")
    lines.append("\nnotes:")
    for n in c.get("notes", []):
        lines.append(f"  - {n}")
    return "\n".join(lines)


