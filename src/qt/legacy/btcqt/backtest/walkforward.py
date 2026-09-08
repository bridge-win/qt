# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Walk-forward splits and parameter-perturbation robustness (v2 plan §6).

- walk_forward: rolling (train, test) windows; strategies here have no auto-fit,
  so 'train' exists to warm indicators and for future calibration hooks —
  metrics are reported on the test windows only, concatenated.
- perturb: re-run the backtest with each key parameter scaled ±30%;
  an edge that dies from a 30% nudge is treated as curve-fit noise.
"""
from __future__ import annotations

import copy
import logging

import pandas as pd

from ..config import Config
from .engine import Backtester
from .metrics import compute_metrics

log = logging.getLogger(__name__)

DAY_MS = 86_400_000


def run_once(cfg: Config, klines: pd.DataFrame, funding: pd.DataFrame,
             oi: pd.DataFrame | None = None) -> dict:
    bt = Backtester(cfg, klines, funding, oi)
    res = bt.run()
    m = compute_metrics(res.equity_ts, res.equity, res.trades, cfg.account.initial_equity)
    m["risk_events"] = [(e.kind, e.ts) for e in res.risk_events]
    return m


def walk_forward(cfg: Config, klines: pd.DataFrame, funding: pd.DataFrame,
                 oi: pd.DataFrame | None = None,
                 train_days: int = 180, test_days: int = 60) -> list[dict]:
    """Returns one metrics dict per test window (plus window bounds)."""
    out = []
    t0, t1 = int(klines.ts.min()), int(klines.ts.max())
    start = t0
    while start + (train_days + test_days) * DAY_MS <= t1:
        test_start = start + train_days * DAY_MS
        test_end = test_start + test_days * DAY_MS
        window = klines[(klines.ts >= start) & (klines.ts < test_end)]
        fund_w = funding[(funding.ts < test_end)]
        bt = Backtester(cfg, window, fund_w, oi)
        res = bt.run()
        # score only the test segment
        seg = [(ts, eq) for ts, eq in zip(res.equity_ts, res.equity) if ts >= test_start]
        seg_trades = [t for t in res.trades if t.exit_ts >= test_start]
        if seg:
            base_eq = seg[0][1]
            m = compute_metrics([s[0] for s in seg], [s[1] for s in seg], seg_trades, base_eq)
            m["window"] = {"test_start": test_start, "test_end": test_end}
            out.append(m)
            log.info("wf window %s -> sharpe=%s trades=%s",
                     pd.Timestamp(test_start, unit='ms', tz='UTC').date(),
                     m.get("sharpe_daily_ann"), m.get("n_trades"))
        start += test_days * DAY_MS
    return out


PERTURB_FIELDS = [
    ("s0", "risk_per_trade"), ("s0", "min_abs_t"),
    ("s0", "stop_atr1h_mult"), ("s0", "cooldown_min"),
    ("s1", "risk_per_trade"), ("s1", "stop_buffer_atr"),
    ("s1", "time_stop_min"), ("s1", "entry_slippage_bps"),
    ("event", "z_trigger"), ("event", "reclaim_min"),
    ("event", "accept_hold_min"), ("composite", "cascade_threshold"),
]


def perturb(cfg: Config, klines: pd.DataFrame, funding: pd.DataFrame,
            oi: pd.DataFrame | None = None, factor: float = 0.3) -> list[dict]:
    """±factor on each production-path parameter, one at a time."""
    results = [{"param": "baseline", "mult": 1.0,
                **_slim(run_once(cfg, klines, funding, oi))}]
    variants: list[tuple[str, Config]] = []
    for section, field in PERTURB_FIELDS:
        for mult in (1 - factor, 1 + factor):
            c = copy.deepcopy(cfg)
            obj = getattr(c, section)
            val = getattr(obj, field)
            setattr(obj, field, type(val)(val * mult) if isinstance(val, int) else val * mult)
            variants.append((f"{section}.{field} x{mult:.1f}", c))
    for name, c in variants:
        m = _slim(run_once(c, klines, funding, oi))
        results.append({"param": name, "mult": None, **m})
        log.info("perturb %-28s sharpe=%s pf=%s trades=%s",
                 name, m["sharpe_daily_ann"], m["profit_factor"], m["n_trades"])
    return results


def _slim(m: dict) -> dict:
    keys = ("sharpe_daily_ann", "profit_factor", "max_drawdown", "total_return", "n_trades")
    return {k: m.get(k) for k in keys}

