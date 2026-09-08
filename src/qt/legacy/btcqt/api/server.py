# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Metrics, authenticated control, dashboard and bounded backtest API."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import lru_cache
import json
import logging
import math
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
from fastapi import (Body, Depends, FastAPI, Header, HTTPException, Query,
                     Request, WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..assessment import ExternalEvidenceCache, build_assessment
from ..backtest.service import BacktestManager
from ..config import Config
from ..control import RuntimeController

log = logging.getLogger(__name__)

HISTORY_METRICS = {
    "price", "ema_anchor", "atr", "atr_1h", "atr_tf", "sigma_1m", "rv_pctl", "dev",
    "vel1", "vel3", "vel5", "ret_z_robust", "vb", "taker_imbalance",
    "liq_count_60s", "liq_notional_60s", "liq_pctl", "liq_oi_ratio", "liq_oi_pctl",
    "doi_5m", "doi_1h", "doi_5m_pctl", "funding_rate", "funding_pctl",
    "open_interest", "open_interest_pctl", "long_short_ratio", "basis_z",
    "trend_score", "trend_agree", "donchian_break", "adx", "xvenue_dev", "spread_pctl",
    "event_active", "event_dir", "event_extreme", "event_vwap", "reclaim_frac",
    "event_verdict", "cascade_score", "cascade_dir", "crowding_score", "eatfear",
    "eatgreed", "fng", "regime", "trend",
}
CHANGE_ONLY_METRICS = {
    "donchian_break", "event_active", "event_dir", "event_verdict",
    "cascade_dir", "regime", "trend",
}
STATE_READY_MAX_AGE_SEC = 90


class StateStore:
    def __init__(self):
        self.snapshot: dict = {}
        self.equity: float | None = None
        self.positions: list[dict] = []
        self.risk: dict = {}
        self.updated_monotonic: float = 0.0
        self._subs: set[asyncio.Queue] = set()

    def update(self, state_dict: dict, equity: float | None = None,
               positions: list[dict] | None = None, risk: dict | None = None) -> None:
        self.snapshot = state_dict
        if equity is not None:
            self.equity = equity
        if positions is not None:
            self.positions = positions
        if risk is not None:
            self.risk = risk
        self.updated_monotonic = time.monotonic()
        payload = self.public_snapshot()
        for q in list(self._subs):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def public_snapshot(self) -> dict:
        return {"state": self.snapshot, "equity": self.equity,
                "positions": self.positions, "risk": self.risk,
                "age_sec": round(time.monotonic() - self.updated_monotonic, 1)
                if self.updated_monotonic else None}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)


class ConfigPatch(BaseModel):
    changes: dict[str, object] = Field(min_length=1)


class BacktestRequest(BaseModel):
    start_ms: int | None = None
    end_ms: int | None = None
    parameters: dict[str, object] = Field(default_factory=dict)


def create_app(store: StateStore, data_dir: Path, token: str = "",
               cors_origins: list[str] | None = None, history_max_rows: int = 20000,
               *, controller: RuntimeController | None = None,
               control_token: str = "", control_token_required: bool = True,
               backtests: BacktestManager | None = None,
               dashboard_enabled: bool = True,
               assessment_config: Config | None = None,
               external_evidence: ExternalEvidenceCache | None = None) -> FastAPI:
    cfg = assessment_config or Config()
    evidence_cache = external_evidence or ExternalEvidenceCache()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await evidence_cache.close()

    app = FastAPI(title="btc-qt production API", docs_url="/api/docs", lifespan=lifespan)
    dashboard_session = secrets.token_urlsafe(32)
    app.add_middleware(CORSMiddleware,
                       allow_origins=cors_origins if cors_origins is not None else [],
                       allow_methods=["GET", "POST", "PATCH"],
                       allow_headers=["Authorization", "Content-Type"])
    rate_buckets: dict[tuple[str, int, bool], int] = {}

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        now_bucket = int(time.time() // 60)
        sensitive = request.url.path.startswith(("/api/v1/control", "/api/v1/config",
                                                  "/api/v1/backtests"))
        key = (request.client.host if request.client else "unknown", now_bucket, sensitive)
        rate_buckets[key] = rate_buckets.get(key, 0) + 1
        if rate_buckets[key] > (60 if sensitive else 300):
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
        if len(rate_buckets) > 2000:
            for old in [k for k in rate_buckets if k[1] < now_bucket - 1]:
                rate_buckets.pop(old, None)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self' ws: wss:"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path in {
            "/", "/assessment", "/app.js", "/assessment.js", "/i18n.js", "/styles.css",
        }:
            response.headers["Cache-Control"] = "no-cache"
        return response

    async def require_read(request: Request, authorization: str | None = Header(default=None)):
        dashboard_cookie = request.cookies.get("btcqt_dashboard") or ""
        same_origin_dashboard = secrets.compare_digest(dashboard_cookie, dashboard_session)
        bearer_authorized = secrets.compare_digest(authorization or "", f"Bearer {token}")
        if token and not same_origin_dashboard and not bearer_authorized:
            raise HTTPException(401, "missing or invalid metrics token")

    async def require_control(authorization: str | None = Header(default=None)):
        if controller is None:
            raise HTTPException(503, "runtime control is unavailable in this process")
        if control_token_required and not control_token:
            raise HTTPException(503, "CONTROL_API_TOKEN is required before controls are enabled")
        if control_token and not secrets.compare_digest(
                authorization or "", f"Bearer {control_token}"):
            raise HTTPException(401, "missing or invalid control token")

    @app.get("/healthz")
    async def healthz():
        age = (time.monotonic() - store.updated_monotonic) if store.updated_monotonic else None
        ok = age is not None and age < 180
        return {"ok": ok, "age_sec": round(age, 1) if age is not None else None,
                "status": "ready" if ok else "stale"}

    @app.get("/readyz")
    async def readyz():
        age = (time.monotonic() - store.updated_monotonic) if store.updated_monotonic else None
        if age is None or age >= STATE_READY_MAX_AGE_SEC:
            raise HTTPException(503, "market data is not fresh")
        if store.risk.get("storage_failed") or store.risk.get("risk_persistence_failed"):
            raise HTTPException(503, "durable state is unhealthy")
        return {"ok": True, "age_sec": round(age, 1)}

    @app.get("/api/v1/snapshot", dependencies=[Depends(require_read)])
    async def snapshot():
        return store.public_snapshot()

    @app.get("/api/v1/assessment", dependencies=[Depends(require_read)])
    async def assessment():
        public = store.public_snapshot()
        as_of_ms = int(public["state"].get("ts") or time.time() * 1000)
        rows = await asyncio.to_thread(
            _load_states, data_dir, as_of_ms - 7 * 86_400_000, as_of_ms,
        )
        return build_assessment(
            public["state"], public["risk"], public["age_sec"], rows, cfg,
            evidence_cache.snapshot(),
        )

    @app.get("/api/v1/history/{metric}", dependencies=[Depends(require_read)])
    async def history(metric: str,
                      from_ms: int | None = Query(default=None, alias="from"),
                      to_ms: int | None = Query(default=None, alias="to"),
                      limit: int = Query(default=10080, le=history_max_rows)):
        if metric not in HISTORY_METRICS:
            raise HTTPException(404, f"unknown metric; available: {sorted(HISTORY_METRICS)}")
        if from_ms is None:
            from_ms = int(time.time() * 1000) - 7 * 86_400_000
        rows = await asyncio.to_thread(_load_states, data_dir, from_ms, to_ms, [metric])
        return _metric_payload(rows, metric, limit)

    @app.get("/api/v1/history", dependencies=[Depends(require_read)])
    async def history_many(metrics: str = Query(default="price,cascade_score,crowding_score,eatfear"),
                           from_ms: int | None = Query(default=None, alias="from"),
                           to_ms: int | None = Query(default=None, alias="to"),
                           limit: int = Query(default=10080, le=history_max_rows)):
        selected = [m.strip() for m in metrics.split(",") if m.strip()]
        unknown = sorted(set(selected) - HISTORY_METRICS)
        selected = [metric for metric in selected if metric in HISTORY_METRICS]
        if not selected:
            raise HTTPException(400, f"unknown or empty metrics: {unknown}")
        if from_ms is None:
            from_ms = int(time.time() * 1000) - 7 * 86_400_000
        if to_ms is None:
            to_ms = int(time.time() * 1000)
        rows = await asyncio.to_thread(_load_states, data_dir, from_ms, to_ms, selected)
        return {
            "series": [_metric_payload(rows, metric, limit) for metric in selected],
            "window": _history_window(rows, from_ms, to_ms),
            "unsupported_metrics": unknown,
        }

    @app.get("/api/v1/config", dependencies=[Depends(require_control)])
    async def get_config():
        return {"parameters": controller.values()}

    @app.patch("/api/v1/config", dependencies=[Depends(require_control)])
    async def patch_config(body: ConfigPatch):
        try:
            values = controller.patch(body.changes)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "parameters": values, "effective_ms": int(time.time() * 1000)}

    @app.post("/api/v1/control/{command}", dependencies=[Depends(require_control)])
    async def command(command: str):
        if command not in {"pause", "resume", "flatten", "kill"}:
            raise HTTPException(404, "unknown control command")
        try:
            detail = await controller.command(command)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "command": command, "detail": detail,
                "effective_ms": int(time.time() * 1000)}

    @app.get("/api/v1/backtests/defaults", dependencies=[Depends(require_read)])
    async def backtest_defaults():
        if backtests is None:
            raise HTTPException(503, "backtests are disabled")
        return backtests.defaults()

    @app.post("/api/v1/backtests", status_code=202,
              dependencies=[Depends(require_control)])
    async def create_backtest(body: BacktestRequest):
        if backtests is None:
            raise HTTPException(503, "backtests are disabled")
        try:
            return await backtests.submit(body.start_ms, body.end_ms, body.parameters)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/backtests/{job_id}", dependencies=[Depends(require_read)])
    async def get_backtest(job_id: str):
        if backtests is None:
            raise HTTPException(503, "backtests are disabled")
        job = backtests.get(job_id)
        if job is None:
            raise HTTPException(404, "backtest job not found")
        return job

    @app.websocket("/api/v1/stream")
    async def stream(ws: WebSocket):
        await ws.accept()
        dashboard_cookie = ws.cookies.get("btcqt_dashboard") or ""
        same_origin_dashboard = secrets.compare_digest(dashboard_cookie, dashboard_session)
        if token and not same_origin_dashboard:
            try:
                auth = await asyncio.wait_for(ws.receive_json(), timeout=5)
            except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
                await ws.close(code=4401)
                return
            if not secrets.compare_digest(str(auth.get("token") or ""), token):
                await ws.close(code=4401)
                return
        q = store.subscribe()
        try:
            await ws.send_text(json.dumps(store.public_snapshot(), default=str))
            while True:
                await ws.send_text(json.dumps(await q.get(), default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            store.unsubscribe(q)

    static_dir = Path(__file__).with_name("static")
    if dashboard_enabled and (static_dir / "index.html").exists():
        def dashboard_response(request: Request, filename: str) -> FileResponse:
            response = FileResponse(static_dir / filename)
            response.set_cookie(
                "btcqt_dashboard",
                dashboard_session,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="strict",
                max_age=8 * 60 * 60,
            )
            return response

        @app.get("/", include_in_schema=False)
        async def dashboard(request: Request):
            return dashboard_response(request, "index.html")

        @app.get("/assessment", include_in_schema=False)
        async def assessment_dashboard(request: Request):
            return dashboard_response(request, "assessment.html")

        @app.get("/app.js", include_in_schema=False)
        async def dashboard_js():
            return FileResponse(static_dir / "app.js", media_type="application/javascript")

        @app.get("/assessment.js", include_in_schema=False)
        async def assessment_js():
            return FileResponse(static_dir / "assessment.js", media_type="application/javascript")

        @app.get("/i18n.js", include_in_schema=False)
        async def dashboard_i18n():
            return FileResponse(static_dir / "i18n.js", media_type="application/javascript")

        @app.get("/styles.css", include_in_schema=False)
        async def dashboard_css():
            return FileResponse(static_dir / "styles.css", media_type="text/css")

        @app.get("/favicon.svg", include_in_schema=False)
        async def dashboard_favicon():
            return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")

    return app


def _json_value(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _metric_payload(rows: pd.DataFrame, metric: str, limit: int) -> dict:
    if rows.empty or metric not in rows.columns:
        return {"metric": metric, "points": []}
    selected = rows[["ts", metric]].dropna()
    if selected.empty:
        return {"metric": metric, "points": []}
    if metric in CHANGE_ONLY_METRICS or not pd.api.types.is_numeric_dtype(selected[metric]):
        selected = selected[selected[metric].ne(selected[metric].shift())]
    if len(selected) > limit:
        step = math.ceil(len(selected) / limit)
        sampled = selected.iloc[::step]
        if int(sampled.iloc[-1]["ts"]) != int(selected.iloc[-1]["ts"]):
            sampled = pd.concat([sampled.iloc[:limit - 1], selected.tail(1)])
        selected = sampled
    return {"metric": metric,
            "points": [[int(t), _json_value(v)] for t, v in selected.itertuples(index=False)]}


def _history_window(rows: pd.DataFrame, from_ms: int, to_ms: int) -> dict:
    if rows.empty:
        return {
            "requested_from": from_ms,
            "requested_to": to_ms,
            "available_from": None,
            "available_to": None,
            "row_count": 0,
            "utc_days": 0,
        }
    timestamps = rows["ts"].astype("int64")
    return {
        "requested_from": from_ms,
        "requested_to": to_ms,
        "available_from": int(timestamps.min()),
        "available_to": int(timestamps.max()),
        "row_count": int(len(rows)),
        "utc_days": int(pd.to_datetime(timestamps, unit="ms", utc=True).dt.date.nunique()),
    }


@lru_cache(maxsize=1024)
def _read_state_frame(path: str, mtime_ns: int, size: int,
                      columns: tuple[str, ...] | None) -> pd.DataFrame:
    del mtime_ns, size  # File version fields are part of the cache key.
    source = Path(path)
    read_columns = None
    if columns is not None:
        names = set(pq.read_schema(source).names)
        read_columns = [column for column in columns if column in names]
        if "ts" not in read_columns:
            return pd.DataFrame()
    return pd.read_parquet(source, columns=read_columns).dropna(axis=1, how="all")


def _load_states(data_dir: Path, from_ms: int | None, to_ms: int | None,
                 columns: list[str] | None = None) -> pd.DataFrame:
    root = Path(data_dir) / "live"
    if not root.exists():
        return pd.DataFrame()
    days = sorted(d for d in root.iterdir() if d.is_dir())
    if from_ms is not None:
        first_day = datetime.fromtimestamp(from_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        days = [day for day in days if day.name >= first_day]
    if to_ms is not None:
        last_day = datetime.fromtimestamp(to_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        days = [day for day in days if day.name <= last_day]
    frames = []
    requested = tuple(dict.fromkeys(["ts", *(columns or [])])) if columns is not None else None
    for day in days:
        for f in sorted(day.glob("states-*.parquet")):
            try:
                file_stat = f.stat()
                frame = _read_state_frame(
                    str(f), file_stat.st_mtime_ns, file_stat.st_size, requested)
                if not frame.empty:
                    frames.append(frame)
            except Exception:
                log.warning("cannot read state journal %s", f, exc_info=True)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts")
    if from_ms is not None:
        df = df[df.ts >= from_ms]
    if to_ms is not None:
        df = df[df.ts <= to_ms]
    return df

