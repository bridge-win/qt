# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""btcqt CLI.

  btcqt backfill  --since 2019-09-08          download klines+funding to parquet
  btcqt calibrate [--start --end]             Phase-1 phenomenon check
  btcqt backtest  [--start --end] [--walkforward] [--perturb]
  btcqt record                                Phase-1 live collection + metrics API
  btcqt paper                                 Phase-3 paper trading (no keys)
  btcqt live --i-understand-live-risk         Phase-4 real trading
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import load_config
from .util.logging import setup_logging

log = logging.getLogger("btcqt")


def _ts(s: str | None) -> int | None:
    if not s:
        return None
    import pandas as pd
    return int(pd.Timestamp(s, tz="UTC").timestamp() * 1000)


def _load_data(cfg, start: str | None, end: str | None):
    from .data.backfill import load_funding, load_klines, load_oi
    kl = load_klines(cfg.data_path, _ts(start), _ts(end))
    fu = load_funding(cfg.data_path)
    if kl.empty:
        sys.exit("no kline data — run `btcqt backfill --since 2019-09-08` first "
                 "(needs internet access to fapi.binance.com)")
    oi = load_oi(cfg.data_path)
    if not oi.empty:
        start_ms, end_ms = _ts(start), _ts(end)
        if start_ms is not None:
            oi = oi[oi.ts >= start_ms]
        if end_ms is not None:
            oi = oi[oi.ts <= end_ms]
    return kl, fu, oi


def _save_report(cfg, name: str, payload) -> Path:
    out = cfg.state_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{name}.json"
    f.write_text(json.dumps(payload, indent=2, default=str))
    return f


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="btcqt", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="config/config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("backfill", help="download historical klines + funding")
    sp.add_argument("--since", default="2019-09-08")

    for name in ("calibrate", "backtest"):
        sp = sub.add_parser(name)
        sp.add_argument("--start")
        sp.add_argument("--end")
        if name == "backtest":
            sp.add_argument("--walkforward", action="store_true")
            sp.add_argument("--perturb", action="store_true")
            sp.add_argument("--stress-slippage", type=float, default=1.0,
                            help="multiply modeled slippage (2.0/3.0 per plan v2.1 gates)")
            sp.add_argument("--delay-bars", type=int, default=0,
                            help="delay market orders N extra candles (latency stress)")
            sp.add_argument("--no-register", action="store_true",
                            help="skip the experiment registry (default: every run is recorded)")

    sub.add_parser("record", help="live data collection + indicators + metrics API")
    sub.add_parser("paper", help="paper trading (simulated fills, no keys)")
    sp = sub.add_parser("live", help="REAL trading")
    sp.add_argument("--i-understand-live-risk", action="store_true", dest="ack")
    sp = sub.add_parser("reset-risk-halt", help="offline reset after documented manual review")
    sp.add_argument("--mode", choices=("paper", "live"), required=True)
    sp.add_argument("--i-reviewed-risk-event", action="store_true", dest="reviewed")

    args = p.parse_args(argv)
    setup_logging("DEBUG" if args.verbose else "INFO")
    cfg = load_config(args.config)

    if args.cmd == "backfill":
        from .data.backfill import run_backfill
        run_backfill(cfg.symbol, cfg.data_path, args.since)

    elif args.cmd == "calibrate":
        from .backtest.calibrate import format_calibration, run_calibration
        kl, fu, _ = _load_data(cfg, args.start, args.end)
        result = run_calibration(cfg, kl, fu)
        f = _save_report(cfg, "calibration", result)
        print(format_calibration(result))
        print(f"\nsaved: {f}")

    elif args.cmd == "backtest":
        from .backtest.deflate import (config_fingerprint, deflated_sharpe,
                                       load_experiments, pbo_estimate, record_experiment)
        from .backtest.engine import Backtester
        from .backtest.metrics import compute_metrics, format_report, gates_verdict
        from .backtest.walkforward import perturb, walk_forward
        kl, fu, oi = _load_data(cfg, args.start, args.end)
        if args.stress_slippage != 1.0:
            cfg.account.stop_slippage_bps *= args.stress_slippage
        bt = Backtester(cfg, kl, fu, oi)
        bt.book.market_delay_bars = args.delay_bars
        res = bt.run()
        m = compute_metrics(res.equity_ts, res.equity, res.trades, cfg.account.initial_equity)
        m["funding_pnl"] = round(res.funding_pnl, 2)
        verdict = gates_verdict(m)
        payload = {"metrics": m, "verdict": verdict,
                   "stress": {"slippage_mult": args.stress_slippage,
                              "delay_bars": args.delay_bars}}
        if args.walkforward:
            payload["walkforward"] = walk_forward(cfg, kl, fu, oi)
        if args.perturb:
            payload["perturb"] = perturb(cfg, kl, fu, oi)
        # ---- experiment registry + multiple-testing honesty (plan v2.1 §4) ----
        span = f"{int(kl.ts.min())}-{int(kl.ts.max())}"
        if not args.no_register:
            entry = {"fingerprint": config_fingerprint(cfg.model_dump(exclude={'secrets'})),
                     "span": span, "sharpe": m.get("sharpe_daily_ann"),
                     "pf": m.get("profit_factor"), "dd": m.get("max_drawdown"),
                     "n_trades": m.get("n_trades"),
                     "stress": payload["stress"],
                     "walkforward_sharpes": [w.get("sharpe_daily_ann")
                                             for w in payload.get("walkforward", [])]}
            record_experiment(cfg.state_path, entry)
        exps = load_experiments(cfg.state_path)
        same_span = [e for e in exps if e.get("span") == span]
        payload["deflated_sharpe"] = deflated_sharpe(
            m.get("sharpe_daily_ann") or 0.0, m.get("days") or 0,
            n_trials=max(len(same_span), 1),
            trial_sharpes=[e.get("sharpe") for e in same_span
                           if isinstance(e.get("sharpe"), (int, float))])
        payload["pbo"] = pbo_estimate(exps)
        dsr_ok = (payload["deflated_sharpe"].get("dsr") or 0) >= 0.95
        pbo_value = payload["pbo"].get("pbo")
        pbo_ok = pbo_value is not None and pbo_value < 0.20
        perturb_ok = bool(payload.get("perturb")) and all(
            (row.get("max_drawdown") or -1) >= -0.08
            for row in payload.get("perturb", []))
        paper_note = "8-week paper and failure-drill gates are external and still required"
        payload["production_verdict"] = {
            "go": False,
            "quantitative_go": bool(verdict["go"] and dsr_ok and pbo_ok and perturb_ok),
            "raw_gates": verdict["go"], "dsr": dsr_ok, "pbo": pbo_ok,
            "perturbation": perturb_ok, "paper": False, "note": paper_note,
        }
        f = _save_report(cfg, "backtest", payload)
        print(format_report(m, verdict))
        ds, pb = payload["deflated_sharpe"], payload["pbo"]
        print(f"\nmultiple-testing check: trials={ds.get('n_trials')} "
              f"DSR={ds.get('dsr')} (gate >=0.95) | PBO={pb.get('pbo')} (gate <0.20)"
              + (f" [{pb.get('note')}]" if pb.get('note') else ""))
        print("production verdict: NO-GO (8-week paper + failure drills still required)")
        if args.walkforward:
            ws = payload["walkforward"]
            ok = sum(1 for w in ws if (w.get("sharpe_daily_ann") or 0) > 0)
            print(f"\nwalk-forward: {ok}/{len(ws)} test windows with positive sharpe")
        if args.perturb:
            print("\nperturbation (edge must survive ±30% nudges):")
            for r in payload["perturb"]:
                print(f"  {r['param']:28s} sharpe={r['sharpe_daily_ann']} "
                      f"pf={r['profit_factor']} dd={r['max_drawdown']} n={r['n_trades']}")
        print(f"\nsaved: {f}")

    elif args.cmd == "reset-risk-halt":
        if not args.reviewed:
            sys.exit("refusing reset: pass --i-reviewed-risk-event after stopping the service")
        from .risk import RiskManager
        risk = RiskManager(cfg.risk, cfg.account.initial_equity,
                           cfg.state_path / f"risk_{args.mode}.json")
        risk.resume(force=True)
        print(f"{args.mode} risk halt reset; review audit before restarting")

    elif args.cmd in ("record", "paper", "live"):
        from .app import TradingApp
        ack = bool(getattr(args, "ack", False))
        if args.cmd == "live" and not ack:
            sys.exit("live mode requires --i-understand-live-risk "
                     "(and LIVE_TRADING=yes in .env, trade-only key, IP whitelist)")
        app = TradingApp(cfg, args.cmd, ack_live=ack)
        asyncio.run(app.run())


if __name__ == "__main__":
    main()

