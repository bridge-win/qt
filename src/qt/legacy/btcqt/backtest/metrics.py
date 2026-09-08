# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Performance metrics from an equity curve + trade list."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..models import Trade


def compute_metrics(equity_ts: list[int], equity: list[float],
                    trades: list[Trade], initial_equity: float) -> dict:
    if not equity:
        return {"error": "empty equity curve"}
    eq = pd.Series(equity, index=pd.to_datetime(equity_ts, unit="ms", utc=True))
    daily = eq.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    total_return = eq.iloc[-1] / initial_equity - 1
    n_days = max((eq.index[-1] - eq.index[0]).days, 1)
    cagr = (eq.iloc[-1] / initial_equity) ** (365 / n_days) - 1 if eq.iloc[-1] > 0 else -1.0
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365)) if len(rets) > 2 and rets.std() > 0 else 0.0
    downside = rets[rets < 0]
    sortino = float(rets.mean() / downside.std() * np.sqrt(365)) if len(downside) > 2 and downside.std() > 0 else 0.0
    peak = eq.cummax()
    dd = (eq / peak - 1.0)
    max_dd = float(dd.min())

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    per_strategy = {}
    for name in sorted({t.strategy for t in trades}):
        st = [t for t in trades if t.strategy == name]
        w = [t for t in st if t.pnl > 0]
        per_strategy[name] = {
            "trades": len(st),
            "win_rate": round(len(w) / len(st), 3) if st else None,
            "pnl": round(sum(t.pnl for t in st), 2),
            "avg_pnl": round(sum(t.pnl for t in st) / len(st), 2) if st else None,
        }

    yearly = {}
    for year, grp in daily.groupby(daily.index.year):
        if len(grp) > 1:
            yearly[int(year)] = round(float(grp.iloc[-1] / grp.iloc[0] - 1), 4)

    return {
        "days": n_days,
        "final_equity": round(float(eq.iloc[-1]), 2),
        "total_return": round(float(total_return), 4),
        "cagr": round(float(cagr), 4),
        "sharpe_daily_ann": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown": round(max_dd, 4),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "n_trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 3) if trades else None,
        "avg_trade_pnl": round(float(np.mean([t.pnl for t in trades])), 2) if trades else None,
        "total_fees": round(sum(t.fees for t in trades), 2),
        "per_strategy": per_strategy,
        "yearly_returns": yearly,
    }


def gates_verdict(m: dict, min_sharpe: float = 1.0, min_pf: float = 1.3,
                  max_dd: float = -0.08) -> dict:
    """Go/no-go gates from v2 plan §6 (parameter-stability & paper-tracking
    gates are evaluated separately)."""
    checks = {
        f"sharpe >= {min_sharpe}": isinstance(m.get("sharpe_daily_ann"), (int, float)) and m["sharpe_daily_ann"] >= min_sharpe,
        f"profit_factor >= {min_pf}": m.get("profit_factor") == "inf" or (isinstance(m.get("profit_factor"), (int, float)) and m["profit_factor"] >= min_pf),
        f"max_drawdown >= {max_dd:.0%}": isinstance(m.get("max_drawdown"), (int, float)) and m["max_drawdown"] >= max_dd,
        "n_trades >= 30": (m.get("n_trades") or 0) >= 30,
    }
    return {"checks": checks, "go": all(checks.values())}


def format_report(m: dict, verdict: dict | None = None) -> str:
    lines = ["=" * 56, "BACKTEST REPORT", "=" * 56]
    for k, v in m.items():
        if k in ("per_strategy", "yearly_returns"):
            continue
        lines.append(f"{k:22s}: {v}")
    lines.append("-" * 56)
    for name, st in (m.get("per_strategy") or {}).items():
        lines.append(f"[{name}] " + "  ".join(f"{k}={v}" for k, v in st.items()))
    if m.get("yearly_returns"):
        lines.append("-" * 56)
        lines.append("yearly: " + "  ".join(f"{y}: {r:+.1%}" for y, r in m["yearly_returns"].items()))
    if verdict is not None:
        lines.append("-" * 56)
        for c, ok in verdict["checks"].items():
            lines.append(f"{'PASS' if ok else 'FAIL':4s}  {c}")
        lines.append(f"VERDICT: {'GO (subject to stability + paper gates)' if verdict['go'] else 'NO-GO'}")
    return "\n".join(lines)

