# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Bounded asynchronous backtest jobs used by the production API."""
from __future__ import annotations

import asyncio
import copy
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import Config
from ..control import HOT_PATHS, _set_dict_path
from ..data.backfill import download_range, load_funding, load_klines, load_oi
from .engine import Backtester
from .deflate import (config_fingerprint, deflated_sharpe, load_experiments,
                      pbo_estimate, record_experiment)
from .metrics import compute_metrics, gates_verdict

DAY_MS = 86_400_000
WARMUP_DAYS = 45


class BacktestManager:
    def __init__(self, cfg: Config, data_dir: Path, max_days: int = 365):
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.max_days = max_days
        self.jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def defaults() -> dict[str, int]:
        now = int(time.time() * 1000)
        end_ms = now - 2 * 60_000
        return {"start_ms": end_ms - 7 * DAY_MS, "end_ms": end_ms}

    async def submit(self, start_ms: int | None, end_ms: int | None,
                     parameters: dict[str, Any] | None = None) -> dict:
        defaults = self.defaults()
        start_ms = int(start_ms or defaults["start_ms"])
        end_ms = int(end_ms or defaults["end_ms"])
        if start_ms >= end_ms:
            raise ValueError("start_ms must be before end_ms")
        if end_ms - start_ms > self.max_days * DAY_MS:
            raise ValueError(f"range exceeds {self.max_days} days")
        if end_ms > int(time.time() * 1000) - 60_000:
            raise ValueError("end_ms must be at least one complete minute in the past")
        params = parameters or {}
        unknown = sorted(set(params) - HOT_PATHS)
        if unknown:
            raise ValueError(f"unsupported parameter(s): {unknown}")
        active = sum(1 for job in self.jobs.values()
                     if job.get("status") in {"queued", "running"})
        if active >= 2:
            raise ValueError("backtest queue is full (one running and one queued allowed)")

        job_id = uuid.uuid4().hex
        job = {"id": job_id, "status": "queued", "created_ms": int(time.time() * 1000),
               "start_ms": start_ms, "end_ms": end_ms, "parameters": params,
               "_config_raw": self.cfg.model_dump(exclude={"secrets"})}
        self.jobs[job_id] = job
        while len(self.jobs) > 20:
            self.jobs.pop(next(iter(self.jobs)))
        asyncio.create_task(self._run(job_id), name=f"backtest-{job_id[:8]}")
        return self.get(job_id)

    async def _run(self, job_id: str) -> None:
        job = self.jobs[job_id]
        async with self._lock:
            job["status"] = "running"
            job["started_ms"] = int(time.time() * 1000)
            try:
                cfg = self._job_config(job["_config_raw"], job["parameters"])
                await self._ensure_data(cfg, job["start_ms"], job["end_ms"])
                result = await asyncio.to_thread(
                    self._compute, cfg, job["start_ms"], job["end_ms"])
                job["result"] = result
                job["status"] = "completed"
            except Exception as exc:
                job["status"] = "failed"
                job["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                job["finished_ms"] = int(time.time() * 1000)

    def _job_config(self, snapshot: dict, parameters: dict[str, Any]) -> Config:
        raw = copy.deepcopy(snapshot)
        for path, value in parameters.items():
            _set_dict_path(raw, path, value)
        return Config.model_validate(raw)

    async def _ensure_data(self, cfg: Config, start_ms: int, end_ms: int) -> None:
        warm_start = start_ms - WARMUP_DAYS * DAY_MS
        existing = load_klines(self.data_dir, warm_start, end_ms)
        expected = max(1, (end_ms - warm_start) // 60_000)
        incomplete = (existing.empty or int(existing.ts.min()) > warm_start + 120_000
                      or int(existing.ts.max()) < end_ms - 120_000
                      or len(existing) < expected * 0.995)
        if incomplete:
            await download_range(cfg.symbol, self.data_dir, warm_start, end_ms)
        verified = load_klines(self.data_dir, warm_start, end_ms)
        if verified.empty or len(verified) < expected * 0.995:
            raise RuntimeError("minute kline coverage is below 99.5% after backfill")

    def _compute(self, cfg: Config, start_ms: int, end_ms: int) -> dict:
        warm_start = start_ms - WARMUP_DAYS * DAY_MS
        klines = load_klines(self.data_dir, warm_start, end_ms)
        if klines.empty:
            raise RuntimeError("minute kline history is unavailable")
        funding = load_funding(self.data_dir)
        funding = funding[(funding.ts >= warm_start) & (funding.ts <= end_ms)]
        oi = load_oi(self.data_dir)
        if not oi.empty:
            oi = oi[(oi.ts >= warm_start) & (oi.ts <= end_ms)]
        bt = Backtester(cfg, klines, funding, oi, trade_start_ms=start_ms)
        res = bt.run()
        metrics = compute_metrics(res.equity_ts, res.equity, res.trades,
                                  cfg.account.initial_equity)
        metrics["funding_pnl"] = round(res.funding_pnl, 2)
        raw_verdict = gates_verdict(metrics)
        span = f"{start_ms}-{end_ms}"
        experiments = load_experiments(cfg.state_path)
        same_span = [e for e in experiments if e.get("span") == span]
        dsr = deflated_sharpe(
            metrics.get("sharpe_daily_ann") or 0.0, metrics.get("days") or 0,
            n_trials=max(len(same_span) + 1, 1),
            trial_sharpes=[e.get("sharpe") for e in same_span
                           if isinstance(e.get("sharpe"), (int, float))])
        entry = {"fingerprint": config_fingerprint(cfg.model_dump(exclude={"secrets"})),
                 "span": span, "sharpe": metrics.get("sharpe_daily_ann"),
                 "pf": metrics.get("profit_factor"), "dd": metrics.get("max_drawdown"),
                 "n_trades": metrics.get("n_trades"), "source": "web-api"}
        record_experiment(cfg.state_path, entry)
        pbo = pbo_estimate(load_experiments(cfg.state_path))
        multiple_ok = ((dsr.get("dsr") or 0) >= 0.95
                       and pbo.get("pbo") is not None and pbo["pbo"] < 0.20)
        step = max(1, len(res.equity) // 1200)
        curve = [[int(ts), round(float(eq), 4)]
                 for ts, eq in zip(res.equity_ts[::step], res.equity[::step])]
        price_window = klines[(klines.ts >= start_ms) & (klines.ts <= end_ms)]
        price_step = max(1, len(price_window) // 1200)
        price_curve = [[int(row.ts), round(float(row.close), 2)]
                       for row in price_window.iloc[::price_step].itertuples(index=False)]
        return {
            "metrics": metrics,
            "verdict": {**raw_verdict, "deflated_sharpe": dsr, "pbo": pbo,
                        "quantitative_go": bool(raw_verdict["go"] and multiple_ok),
                        "production_go": False,
                        "note": "NO-GO until raw, DSR, PBO, perturbation and paper gates all pass."},
            "equity_curve": curve,
            "price_curve": price_curve,
            "trades": [asdict(t) for t in res.trades[-500:]],
            "risk_events": [asdict(e) for e in res.risk_events],
            "data_quality": {
                "minute_klines": True,
                "funding": not funding.empty,
                "historical_open_interest": not oi.empty,
                "liquidations": False,
                "note": "Missing microstructure feeds force the wick classifier into its stricter degraded-data path; S2 is disabled by default.",
            },
        }

    def get(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if not job:
            return None
        public = copy.deepcopy(job)
        public.pop("_config_raw", None)
        return public

