"""Dependency-free local dashboard server."""

from __future__ import annotations

import html
import json
import math
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypeAlias, cast
from urllib.parse import parse_qs, urlparse

import pandas as pd
from btc_backtest.data.models import DataRequest, MarketBundle
from btc_backtest.engine.models import InstrumentKind, OrderIntent, OrderSide, OrderType
from btc_backtest.engine.runner import EventRunner
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.registry import default_strategy_registry

from qt.backtest.artifacts import latest_backtest_summary, write_backtest_artifacts
from qt.backtest.engine import Backtester
from qt.backtest.metrics import Metrics, compute_metrics
from qt.backtest.strategy_backtest import (
    SUPPORTED,
    _equity_series,
    _infer_timeframe,
    _market_dataset,
    _normalize_trades,
    _request_for,
    _spec,
    _trades_frame,
    run_strategy_backtest,
    write_strategy_backtest_artifacts,
)
from qt.backtest.validation import ohlcv_fingerprint
from qt.core.config import load_settings
from qt.dashboard.learning import render_learning_page
from qt.dashboard.platform import build_trading_demo, render_demo_page, render_platform_page
from qt.data.catalog import data_source_statuses
from qt.data.store import ParquetStore
from qt.intel.ranker import read_opportunities
from qt.monitoring.state import MonitorStateStore
from qt.portfolio import read_all_portfolios, read_portfolio
from qt.research.datasets import DatasetCatalog
from qt.research.repository import ResearchRepository
from qt.research.service import normalize_job_request

JsonDict: TypeAlias = dict[str, object]
_BACKTEST_STRATEGIES = ("composite", *SUPPORTED)
_COMPARE_STRATEGIES = SUPPORTED
_CHART_MAX_POINTS = 900
_JOB_STAGE_NAMES = (
    "queued",
    "validation",
    "indicators",
    "simulation",
    "metrics",
    "robustness",
    "visualization",
    "complete",
)
_MAX_BACKTEST_QUEUE = 5
_RECIPE_INDICATORS = {
    "close_above_sma",
    "close_below_sma",
    "rsi_below",
    "rsi_above",
    "bollinger_lower_touch",
    "bollinger_upper_touch",
    "donchian_breakout",
    "atr_breakout",
}
_STRATEGY_GROUPS: dict[str, tuple[str, ...]] = {
    "accumulation": ("fixed_dca", "smart_dca"),
    "trend": (
        "sma_crossover",
        "ema_crossover",
        "macd_trend",
        "donchian_breakout",
        "turtle_trend",
        "time_series_momentum",
        "dual_momentum",
        "rate_of_change",
        "adx_trend",
        "atr_volatility_breakout",
        "keltner_channel",
    ),
    "mean_reversion": (
        "rsi_mean_reversion",
        "stochastic_reversal",
        "bollinger_mean_reversion",
        "vwap_mean_reversion",
        "grid_rebalance",
    ),
    "breakout": ("bollinger_breakout", "donchian_breakout", "turtle_trend"),
    "benchmark": ("buy_and_hold",),
    "derivatives": ("funding_basis_carry",),
    "qt_special": ("capitulation", "wick_catcher"),
}
_BEGINNER_STRATEGIES = {
    "buy_and_hold",
    "fixed_dca",
    "smart_dca",
    "sma_crossover",
    "rsi_mean_reversion",
    "bollinger_mean_reversion",
    "donchian_breakout",
}
_STRATEGY_PLAIN_GUIDE: dict[str, str] = {
    "fixed_dca": "Buy a fixed amount on a schedule. It tests disciplined accumulation rather than market timing.",
    "smart_dca": "Accumulate more aggressively when valuation or fear signals are favorable, then slow down in hotter regimes.",
    "sma_crossover": "Hold BTC when a fast moving average is above a slow moving average, otherwise stay in cash.",
    "ema_crossover": "Like SMA crossover, but recent candles count more, so it reacts faster and can whipsaw more.",
    "macd_trend": "Use MACD momentum to follow sustained upside and exit when momentum deteriorates.",
    "rsi_mean_reversion": "Buy oversold weakness and sell recovery. It assumes BTC rebounds after stretched downside.",
    "stochastic_reversal": "Use stochastic oscillator extremes to test short-term reversal entries and exits.",
    "bollinger_mean_reversion": "Buy near lower volatility bands and exit near the middle or upper band.",
    "bollinger_breakout": "Buy strength when price breaks out of its volatility envelope.",
    "donchian_breakout": "Buy new highs over a lookback window and exit when price loses the channel.",
    "turtle_trend": "Classic breakout trend following with channel exits and volatility-aware behavior.",
    "time_series_momentum": "Hold BTC when its own trailing return is positive; move to cash when it is not.",
    "dual_momentum": "Compare BTC momentum against a defensive cash stance and only hold BTC when momentum is strong.",
    "rate_of_change": "Use the speed of price change as the entry and exit signal.",
    "adx_trend": "Only trade when trend strength is high enough, reducing trades in choppy ranges.",
    "atr_volatility_breakout": "Use volatility-adjusted breakout thresholds so entries adapt to quieter or wilder markets.",
    "keltner_channel": "Use ATR-based channels to trade trend continuation while accounting for volatility.",
    "vwap_mean_reversion": "Test whether price stretched away from VWAP tends to revert.",
    "grid_rebalance": "Rebalance around price bands. It prefers ranging markets and suffers in one-way trends.",
    "funding_basis_carry": "Pair spot and perpetual positions to collect funding. It needs derivative data and is not beginner default.",
    "buy_and_hold": "Benchmark: buy BTC once and hold. Every active strategy should be compared against this.",
    "capitulation": "QT special: look for extreme crash conditions before entering.",
    "wick_catcher": "QT special: buy sharp downside wicks and sell rebounds.",
}
_STRATEGY_GUIDE: dict[str, dict[str, str]] = {
    "composite": {
        "label": "Composite signal engine",
        "plain": (
            "Combines price action, sentiment, derivatives, on-chain and macro "
            "inputs into QT's risk-gated BTC timing model."
        ),
        "best_for": "Understanding the full QT thesis before paper trading.",
        "risk": "Can sit out for long periods; missing auxiliary data weakens the signal.",
    },
    "dca": {
        "label": "Smart DCA",
        "plain": (
            "Buys BTC in staged allocations instead of all at once, with optional "
            "fear/valuation context when local data exists."
        ),
        "best_for": "Learning position accumulation and drawdown tolerance.",
        "risk": "Usually keeps market exposure, so large BTC drawdowns still matter.",
    },
    "trend": {
        "label": "Trend following",
        "plain": "Uses moving-average trend logic to join strength and step aside from weakness.",
        "best_for": "Testing whether a simple momentum rule survives BTC chop.",
        "risk": "Can whipsaw when the market ranges sideways.",
    },
    "carry": {
        "label": "Funding / basis carry",
        "plain": (
            "Looks for futures funding or basis carry opportunities using local "
            "funding data when available."
        ),
        "best_for": "Studying derivative yield rather than pure price direction.",
        "risk": "Funding data gaps and exchange-specific execution risk can dominate results.",
    },
    "wick": {
        "label": "Wick catcher",
        "plain": "Looks for sharp intrabar downside wicks and rebound behavior.",
        "best_for": "Testing crash-recovery entries and laddered dip buying.",
        "risk": "Falling-knife entries can compound losses during real breakdowns.",
    },
}


class _RuleRecipeStrategy:
    def __init__(self, strategy_id: str, rules: Mapping[str, object]) -> None:
        self.rules = dict(rules)
        self.metadata = StrategyMetadata(
            id=strategy_id,
            version="1.0.0",
            description="No-code dashboard rule recipe.",
            warmup_bars=_recipe_warmup(rules),
            supported_timeframes=("1h", "1d"),
            supported_instruments=(InstrumentKind.SPOT,),
            parameter_schema={},
        )

    def initialize(self, context: InitializationContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        spot = context.portfolio.position(InstrumentKind.SPOT)
        if spot.quantity > 0:
            if _rule_group_matches(context.bars, self.rules.get("exit"), default=False):
                return (
                    OrderIntent(
                        instrument=InstrumentKind.SPOT,
                        side=OrderSide.SELL,
                        order_type=OrderType.MARKET,
                        base_quantity=spot.quantity,
                        reason="recipe_exit",
                    ),
                )
            return ()
        if _rule_group_matches(context.bars, self.rules.get("entry"), default=False):
            return (
                OrderIntent(
                    instrument=InstrumentKind.SPOT,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quote_amount=context.portfolio.equity * Decimal("0.95"),
                    reason="recipe_entry",
                ),
            )
        return ()

    def finalize(self, context: FinalizationContext) -> None:
        return None


@dataclass(frozen=True)
class DashboardContext:
    parquet_dir: Path
    backtests_dir: Path
    monitor_state_path: Path
    strategies_state_dir: Path
    runtime_dir: Path


def serve_dashboard(
    *,
    host: str,
    port: int,
    parquet_dir: str | Path,
    backtests_dir: str | Path,
    monitor_state_path: str | Path,
    strategies_state_dir: str | Path | None = None,
    runtime_dir: str | Path | None = None,
) -> None:
    context = DashboardContext(
        parquet_dir=Path(parquet_dir),
        backtests_dir=Path(backtests_dir),
        monitor_state_path=Path(monitor_state_path),
        strategies_state_dir=Path(
            strategies_state_dir
            or Path(monitor_state_path).parent / "strategies"
        ),
        runtime_dir=Path(runtime_dir or Path(monitor_state_path).parent),
    )
    handler = _make_handler(context)
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _make_handler(context: DashboardContext) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/static/lightweight-charts-5.2.0.js":
                asset = (
                    Path(__file__).parent
                    / "static"
                    / "lightweight-charts.standalone.production.js"
                )
                if not asset.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND, "chart asset not found")
                    return
                self._send_bytes(
                    asset.read_bytes(),
                    content_type="text/javascript; charset=utf-8",
                )
                return
            if path == "/":
                self._send_html(_render_home(context))
                return
            if path == "/learn":
                self._send_html(render_learning_page())
                return
            if path == "/platform":
                self._send_html(render_platform_page())
                return
            if path == "/demo":
                self._send_html(render_demo_page())
                return
            if path == "/backtest":
                self._send_html(_render_backtest_page(context))
                return
            if path == "/backtest/build":
                self._send_html(_render_backtest_builder_page(context))
                return
            if path.startswith("/backtest/runs/"):
                run_id = path[len("/backtest/runs/"):].strip("/")
                summary = _research_run_summary(context, run_id)
                if summary is None:
                    self.send_error(HTTPStatus.NOT_FOUND, f"no backtest run for {run_id}")
                    return
                self._send_html(_render_research_run_page(context, summary))
                return
            if path == "/api/sources":
                self._send_json({"sources": _sources(context)})
                return
            if path == "/api/demo":
                self._send_json({"demo": build_trading_demo()})
                return
            if path == "/api/backtests/latest":
                self._send_json({"backtest": _latest_backtest(context)})
                return
            if path == "/api/monitor":
                self._send_json({"monitor": _monitor(context)})
                return
            if path == "/api/strategies":
                self._send_json({"strategies": _strategies(context)})
                return
            if path == "/api/intel":
                self._send_json({"intel": _intel(context)})
                return
            if path == "/api/portfolios":
                self._send_json({"portfolios": _portfolios(context)})
                return
            if path == "/api/backtests/options":
                self._send_json(_backtest_options(context))
                return
            if path == "/api/v1/backtest/catalog":
                self._send_json(_backtest_catalog(context))
                return
            if path == "/api/v2/backtests/catalog":
                self._send_json(_v2_backtest_catalog(context))
                return
            if path == "/api/v2/backtests/datasets":
                self._send_json(
                    {"datasets": _managed_dataset_catalog(context).list_datasets()}
                )
                return
            if path == "/api/v2/backtests/health":
                self._send_json(_research_health(context))
                return
            if path == "/api/v2/backtests/runs":
                self._send_json(
                    {"runs": _research_repository(context).list_runs()}
                )
                return
            if path.startswith("/api/v2/backtests/jobs/"):
                job_id = path[len("/api/v2/backtests/jobs/"):].strip("/")
                if "/" in job_id:
                    self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                    return
                try:
                    job = _research_repository(context).get_job(job_id)
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND, f"no backtest job for {job_id}")
                    return
                self._send_json({"job": job})
                return
            if path.startswith("/api/v2/backtests/runs/"):
                suffix = path[len("/api/v2/backtests/runs/"):].strip("/")
                parts = suffix.split("/")
                run_id = parts[0]
                try:
                    run = _research_repository(context).get_run(run_id)
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND, f"no backtest run for {run_id}")
                    return
                if len(parts) == 2 and parts[1] == "series":
                    self._send_json(
                        {"run_id": run_id, **_v2_run_series(context, run)}
                    )
                    return
                if len(parts) == 3 and parts[1] == "artifacts":
                    artifact_name = parts[2]
                    allowed = run.get("artifacts")
                    if (
                        not _is_opaque_run_id(run_id)
                        or
                        not isinstance(allowed, list)
                        or artifact_name not in allowed
                        or artifact_name not in {
                            "data_manifest.json",
                            "equity.csv",
                            "summary.json",
                            "trades.csv",
                        }
                    ):
                        self.send_error(HTTPStatus.NOT_FOUND, "artifact not found")
                        return
                    artifact = context.backtests_dir / run_id / artifact_name
                    if not artifact.is_file():
                        self.send_error(HTTPStatus.NOT_FOUND, "artifact not found")
                        return
                    content_type = (
                        "application/json; charset=utf-8"
                        if artifact.suffix == ".json"
                        else "text/csv; charset=utf-8"
                    )
                    self._send_bytes(artifact.read_bytes(), content_type=content_type)
                    return
                if len(parts) != 1:
                    self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                    return
                self._send_json({"run": run})
                return
            if path.startswith("/api/v1/backtest/jobs/"):
                job_id = path[len("/api/v1/backtest/jobs/"):].strip("/")
                legacy_job = _read_backtest_job(context, job_id)
                if legacy_job is None:
                    self.send_error(HTTPStatus.NOT_FOUND, f"no backtest job for {job_id}")
                    return
                self._send_json({"job": legacy_job})
                return
            if path.startswith("/api/v1/backtest/runs/"):
                run_id = path[len("/api/v1/backtest/runs/"):].strip("/")
                summary = _research_run_summary(context, run_id)
                if summary is None:
                    self.send_error(HTTPStatus.NOT_FOUND, f"no backtest run for {run_id}")
                    return
                self._send_json({"run": summary})
                return
            if path == "/intel":
                self._send_html(_render_intel_page(context))
                return
            if path == "/portfolio":
                self._send_html(_render_portfolio_overview(context))
                return
            if path.startswith("/api/portfolio/"):
                name = path[len("/api/portfolio/"):].strip("/")
                pf = _portfolio(context, name)
                if pf is None:
                    self.send_error(HTTPStatus.NOT_FOUND, f"no portfolio for {name}")
                    return
                self._send_json({"portfolio": pf})
                return
            if path.startswith("/api/strategy/"):
                name = path[len("/api/strategy/"):].strip("/")
                snap = _strategy(context, name)
                if snap is None:
                    self.send_error(HTTPStatus.NOT_FOUND, f"no state for {name}")
                    return
                self._send_json({"strategy": snap})
                return
            if path.startswith("/strategy/"):
                name = path[len("/strategy/"):].strip("/")
                snap = _strategy(context, name)
                if snap is None:
                    self.send_error(HTTPStatus.NOT_FOUND, f"no state for {name}")
                    return
                pf = _portfolio(context, name)
                self._send_html(_render_strategy_detail(name, snap, pf))
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path.startswith("/api/v2/backtests/"):
                if self.headers.get_content_type() != "application/json":
                    self._send_json(
                        {"ok": False, "errors": ["application/json is required"]},
                        status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    )
                    return
                origin = self.headers.get("Origin")
                host = self.headers.get("Host", "")
                if origin and urlparse(origin).netloc != host:
                    self._send_json(
                        {"ok": False, "errors": ["cross-origin submission rejected"]},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
            if (
                path.startswith("/api/v2/backtests/datasets/")
                and path.endswith("/sync")
            ):
                dataset_id = path[
                    len("/api/v2/backtests/datasets/"):-len("/sync")
                ].strip("/")
                if "/" in dataset_id:
                    self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                    return
                try:
                    dataset = _managed_dataset_catalog(context).get(dataset_id)
                    if dataset.get("provider") != "bitstamp":
                        raise ValueError(
                            f"dataset does not support synchronization: {dataset_id}"
                        )
                    repository = _research_repository(context)
                    _enforce_research_rate_limit(repository)
                    job = repository.enqueue(
                        {"kind": "dataset_sync", "dataset_id": dataset_id}
                    )
                except ResearchRateLimitError as error:
                    self._send_json(
                        {"ok": False, "errors": [str(error)]},
                        status=HTTPStatus.TOO_MANY_REQUESTS,
                    )
                    return
                except (KeyError, ValueError) as error:
                    self._send_json(
                        {"ok": False, "errors": [str(error)]},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                self._send_json(
                    {"ok": True, "job": job},
                    status=HTTPStatus.ACCEPTED,
                )
                return
            if path == "/api/v2/backtests/jobs":
                payload = self._read_json_body()
                if payload.get("_error"):
                    self._send_json(
                        {"ok": False, "errors": [str(payload["_error"])]},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                try:
                    spec = normalize_job_request(
                        payload,
                        _managed_dataset_catalog(context),
                    )
                    repository = _research_repository(context)
                    _enforce_research_rate_limit(repository)
                    job = repository.enqueue(spec)
                except ResearchRateLimitError as error:
                    self._send_json(
                        {"ok": False, "errors": [str(error)]},
                        status=HTTPStatus.TOO_MANY_REQUESTS,
                    )
                    return
                except (KeyError, ValueError) as error:
                    self._send_json(
                        {"ok": False, "errors": [str(error)]},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                self._send_json(
                    {"ok": True, "job": job},
                    status=HTTPStatus.ACCEPTED,
                )
                return
            if (
                path.startswith("/api/v2/backtests/jobs/")
                and path.endswith("/cancel")
            ):
                job_id = path[
                    len("/api/v2/backtests/jobs/"):-len("/cancel")
                ].strip("/")
                try:
                    job = _research_repository(context).request_cancel(job_id)
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND, f"no backtest job for {job_id}")
                    return
                self._send_json({"ok": True, "job": job})
                return
            if path == "/api/v1/backtest/recipes/validate":
                payload = self._read_json_body()
                validation = _validate_backtest_recipe(context, payload)
                status = HTTPStatus.OK if validation["ok"] is True else HTTPStatus.BAD_REQUEST
                self._send_json(validation, status=status)
                return
            if path == "/api/v1/backtest/jobs":
                payload = self._read_json_body()
                response = _submit_backtest_job(context, payload)
                status = HTTPStatus.ACCEPTED if response["ok"] is True else HTTPStatus.BAD_REQUEST
                self._send_json(response, status=status)
                return
            if path != "/backtest/run":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 8192:
                self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "form too large")
                return
            raw_body = self.rfile.read(length).decode("utf-8")
            fields = parse_qs(raw_body, keep_blank_values=True)
            result = _run_backtest_request(context, fields)
            status = HTTPStatus.OK if result.get("ok") is True else HTTPStatus.BAD_REQUEST
            self._send_html(_render_backtest_page(context, result), status=status)

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send_html(self, body: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, body: JsonDict, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = json.dumps(body, indent=2, sort_keys=True, default=str).encode("utf-8")
            self._send_bytes(
                payload,
                content_type="application/json; charset=utf-8",
                status=status,
            )

        def _send_bytes(
            self,
            payload: bytes,
            *,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _read_json_body(self) -> JsonDict:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 65536:
                return {"_error": "json body too large"}
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return {"_error": "invalid json"}
            return payload if isinstance(payload, dict) else {"_error": "json body must be an object"}

    return DashboardHandler


def _backtest_catalog(context: DashboardContext) -> JsonDict:
    registry = default_strategy_registry()
    strategies: list[JsonDict] = []
    for strategy_id in registry.list():
        metadata = registry.describe(strategy_id)
        parameter_schema = metadata.model_dump(mode="json").get("parameter_schema", {})
        strategies.append(
            {
                "id": strategy_id,
                "name": strategy_id.replace("_", " ").title(),
                "description": metadata.description,
                "principle": metadata.description,
                "explain_like_beginner": _STRATEGY_PLAIN_GUIDE.get(
                    strategy_id,
                    metadata.description,
                ),
                "suitable_market_regime": _strategy_regime(strategy_id),
                "failure_mode": _strategy_failure_mode(strategy_id),
                "risk_level": _strategy_risk_level(strategy_id),
                "groups": _groups_for_strategy(strategy_id),
                "beginner_friendly": strategy_id in _BEGINNER_STRATEGIES,
                "warmup_bars": metadata.warmup_bars,
                "supported_timeframes": list(metadata.supported_timeframes),
                "supported_instruments": [
                    instrument.value for instrument in metadata.supported_instruments
                ],
                "parameter_schema": parameter_schema,
                "parameter_guide": _parameter_guide(parameter_schema),
                "risk_notes": _risk_notes(strategy_id),
                "presets": _strategy_presets(strategy_id),
                "runnable_now": _is_spot_only(strategy_id),
            }
        )
    return {
        "strategies": strategies,
        "groups": _STRATEGY_GROUPS,
        "data": {
            "standard": {
                "provider": "bitstamp",
                "symbol": "BTC/USD",
                "timeframe": "1d",
                "years": 10,
                "badge": (
                    "10-year standard"
                    if any(
                        item.get("standard_ready") is True
                        for item in _managed_dataset_catalog(context).list_datasets()
                    )
                    else None
                ),
                "note": (
                    "Use this for serious BTC spot research only after the "
                    "local manifest passes validation."
                ),
            },
            "available_ohlcv_keys": _available_ohlcv_options(context),
            "exchange_specific": {
                "provider": "okx",
                "symbol": "BTC/USDT",
                "note": "Use separately from BTC/USD; do not stitch USD and USDT datasets.",
            },
        },
        "recipe_builder": {
            "max_entry_conditions": 3,
            "operators": ("ALL", "ANY"),
            "indicators": sorted(_RECIPE_INDICATORS),
            "market": "spot long/cash",
        },
        "defaults": {
            "strategy_id": "sma_crossover",
            "ohlcv_key": _default_ohlcv_key(context),
            "initial_cash": 10_000,
            "fee_bps": 10,
            "slippage_bps": 5,
            "seed": 7,
        },
    }


def _v2_backtest_catalog(context: DashboardContext) -> JsonDict:
    catalog = _backtest_catalog(context)
    catalog["api_version"] = "2"
    catalog["modes"] = ["template", "custom_rules", "ensemble"]
    catalog["validation_profiles"] = {
        "quick": "Single replay plus buy-and-hold and fixed-DCA benchmarks.",
        "standard": (
            "Purged walk-forward, sensitivity, 500 seeded bootstrap paths, "
            "cost stress, and bias/stability gates."
        ),
    }
    catalog["datasets"] = _managed_dataset_catalog(context).list_datasets()
    return catalog


def _research_repository(context: DashboardContext) -> ResearchRepository:
    return ResearchRepository(context.backtests_dir / "research.sqlite3")


def _managed_dataset_catalog(context: DashboardContext) -> DatasetCatalog:
    repository = _research_repository(context)
    return DatasetCatalog(
        context.parquet_dir,
        syncing_ids=repository.syncing_dataset_ids(),
    )


class ResearchRateLimitError(ValueError):
    pass


def _enforce_research_rate_limit(repository: ResearchRepository) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    if repository.submissions_since(cutoff) >= 10:
        raise ResearchRateLimitError(
            "research submission limit reached: ten jobs per hour"
        )


def _research_health(context: DashboardContext) -> JsonDict:
    repository = _research_repository(context)
    datasets = _managed_dataset_catalog(context).list_datasets()
    standard_ready = any(
        item.get("standard_ready") is True for item in datasets
    )
    counts = repository.queue_counts()
    worker = repository.worker_status(online_within=timedelta(seconds=15))
    return {
        "status": (
            "healthy"
            if standard_ready and worker.get("online") is True
            else "degraded"
        ),
        "worker": {**counts, **worker},
        "data": {
            "standard_ready": standard_ready,
            "ready_datasets": sum(
                1 for item in datasets if item.get("status") == "ready"
            ),
        },
        "live_trading": False,
    }


def _groups_for_strategy(strategy_id: str) -> list[str]:
    return [
        group
        for group, strategy_ids in _STRATEGY_GROUPS.items()
        if strategy_id in strategy_ids
    ]


def _parameter_guide(schema: object) -> list[JsonDict]:
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    guide: list[JsonDict] = []
    for name, raw in properties.items():
        if not isinstance(raw, dict):
            continue
        default = raw.get("default", "")
        guide.append(
            {
                "name": str(name),
                "default": default,
                "impact": _parameter_impact(str(name)),
            }
        )
    return guide


def _parameter_impact(name: str) -> str:
    if "fast" in name or "short" in name:
        return "Lower values react faster and create more trades; higher values react slower."
    if "slow" in name or "long" in name or "window" in name or "period" in name:
        return "Higher values need more history and reduce noise, but signals arrive later."
    if "threshold" in name or "entry" in name or "exit" in name:
        return "Tighter thresholds trade more often; wider thresholds wait for stronger evidence."
    if "weight" in name or "allocation" in name:
        return "Higher values increase return potential and drawdown at the same time."
    return "Change this only one step at a time, then compare return, drawdown, and trade count."


def _risk_notes(strategy_id: str) -> list[str]:
    groups = set(_groups_for_strategy(strategy_id))
    notes = [
        "Backtests do not predict tomorrow; they show how rules behaved on historical candles.",
        "Always compare against buy_and_hold and inspect max drawdown before total return.",
    ]
    if "trend" in groups or "breakout" in groups:
        notes.append("Trend rules can lose through repeated whipsaws in sideways markets.")
    if "mean_reversion" in groups:
        notes.append("Mean-reversion rules can keep buying into a real breakdown.")
    if "derivatives" in groups:
        notes.append("Derivative strategies need funding, basis, liquidity, and exchange-risk checks.")
    if strategy_id == "grid_rebalance":
        notes.append("Grid systems can look strong in ranges and weak in one-way markets.")
    return notes


def _strategy_regime(strategy_id: str) -> str:
    groups = set(_groups_for_strategy(strategy_id))
    if strategy_id in {"buy_and_hold", "fixed_dca", "smart_dca"}:
        return "Long-horizon accumulation across mixed market regimes."
    if "trend" in groups or "breakout" in groups:
        return "Persistent directional markets with enough movement to cover costs."
    if "mean_reversion" in groups or strategy_id == "grid_rebalance":
        return "Sideways or oscillating markets without a structural breakdown."
    if "derivatives" in groups:
        return "Specialized basis and funding conditions with complete derivatives data."
    return "Regime-dependent; compare several market cycles before drawing conclusions."


def _strategy_failure_mode(strategy_id: str) -> str:
    groups = set(_groups_for_strategy(strategy_id))
    if strategy_id == "buy_and_hold":
        return "It remains fully exposed through deep multi-year drawdowns."
    if "trend" in groups or "breakout" in groups:
        return "Repeated false signals can compound losses in choppy markets."
    if "mean_reversion" in groups:
        return "A genuine trend can keep moving against the assumed reversion."
    if strategy_id in {"fixed_dca", "smart_dca"}:
        return "Accumulation does not protect capital when the asset falls permanently."
    if "derivatives" in groups:
        return "Missing funding, liquidity, or liquidation mechanics can invalidate results."
    return "Performance may depend on a narrow historical regime or sensitive parameters."


def _strategy_risk_level(strategy_id: str) -> str:
    if strategy_id in _BEGINNER_STRATEGIES:
        return "beginner"
    if strategy_id in {
        "funding_basis_carry",
        "grid_rebalance",
        "capitulation",
        "wick_catcher",
    }:
        return "advanced or data-constrained"
    return "intermediate"


def _strategy_presets(strategy_id: str) -> list[JsonDict]:
    presets: dict[str, list[JsonDict]] = {
        "buy_and_hold": [{"name": "Full BTC", "parameters": {"allocation": "1"}}],
        "fixed_dca": [{"name": "Weekly $100", "parameters": {"quote_amount": "100"}}],
        "sma_crossover": [
            {
                "name": "Classic 50/200",
                "parameters": {"fast_window": 50, "slow_window": 200},
            }
        ],
        "rsi_mean_reversion": [
            {
                "name": "Balanced 14/30/55",
                "parameters": {"window": 14, "entry": "30", "exit": "55"},
            }
        ],
        "bollinger_mean_reversion": [
            {
                "name": "Classic 20/2",
                "parameters": {"window": 20, "stddev": "2"},
            }
        ],
        "donchian_breakout": [
            {
                "name": "Classic 20/10",
                "parameters": {"entry_window": 20, "exit_window": 10},
            }
        ],
    }
    return presets.get(strategy_id, [])


def _is_spot_only(strategy_id: str) -> bool:
    metadata = default_strategy_registry().describe(strategy_id)
    return all(
        instrument == InstrumentKind.SPOT
        for instrument in metadata.supported_instruments
    )


def _validate_backtest_recipe(
    context: DashboardContext,
    payload: Mapping[str, object],
) -> JsonDict:
    errors = _validate_backtest_payload(context, payload)
    rules = payload.get("rules")
    if isinstance(rules, dict):
        errors.extend(_validate_rule_group(rules.get("entry"), "entry"))
        errors.extend(_validate_rule_group(rules.get("exit"), "exit"))
    if errors:
        return {"ok": False, "errors": errors}
    return {
        "ok": True,
        "errors": [],
        "normalized": _normalized_backtest_spec(context, payload),
        "explanation": (
            "The recipe is valid for spot long/cash research. It can be run as a "
            "research job, then compared against buy_and_hold before paper trading."
        ),
    }


def _validate_rule_group(raw: object, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"{label} rules must be an object"]
    operator = str(raw.get("operator", "")).upper()
    if operator not in {"ALL", "ANY"}:
        return [f"{label} operator must be ALL or ANY"]
    conditions = raw.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return [f"{label} rules require at least one condition"]
    if len(conditions) > 3:
        return [f"{label} rules support at most 3 conditions"]
    errors: list[str] = []
    for index, condition in enumerate(conditions, start=1):
        if not isinstance(condition, dict):
            errors.append(f"{label} condition {index} must be an object")
            continue
        indicator = str(condition.get("indicator", ""))
        if indicator not in _RECIPE_INDICATORS:
            errors.append(f"{label} condition {index} has unsupported indicator: {indicator}")
    return errors


def _recipe_has_conditions(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    for key in ("entry", "exit"):
        group = raw.get(key)
        if not isinstance(group, dict):
            continue
        conditions = group.get("conditions")
        if isinstance(conditions, list) and conditions:
            return True
    return False


def _recipe_warmup(rules: Mapping[str, object]) -> int:
    windows: list[int] = []
    for group_name in ("entry", "exit"):
        group = rules.get(group_name)
        if not isinstance(group, dict):
            continue
        conditions = group.get("conditions")
        if not isinstance(conditions, list):
            continue
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            windows.append(_condition_window(condition))
    return max(windows, default=2) + 2


def _rule_group_matches(frame: pd.DataFrame, raw: object, *, default: bool) -> bool:
    if not isinstance(raw, dict):
        return default
    conditions = raw.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return default
    values = [
        _condition_matches(frame, condition)
        for condition in conditions
        if isinstance(condition, dict)
    ]
    if not values:
        return default
    operator = str(raw.get("operator", "ALL")).upper()
    return any(values) if operator == "ANY" else all(values)


def _condition_matches(frame: pd.DataFrame, condition: Mapping[str, object]) -> bool:
    if frame.empty or "close" not in frame.columns:
        return False
    indicator = str(condition.get("indicator", ""))
    window = _condition_window(condition)
    if len(frame) < max(2, window):
        return False
    close = pd.to_numeric(frame["close"], errors="coerce")
    current = _finite_float(close.iloc[-1])
    if current is None:
        return False
    if indicator == "close_above_sma":
        average = _finite_float(close.tail(window).mean())
        return average is not None and current > average
    if indicator == "close_below_sma":
        average = _finite_float(close.tail(window).mean())
        return average is not None and current < average
    if indicator in {"rsi_below", "rsi_above"}:
        threshold = _condition_threshold(condition, 30 if indicator == "rsi_below" else 70)
        rsi = _latest_rsi(close, window)
        if rsi is None:
            return False
        return rsi < threshold if indicator == "rsi_below" else rsi > threshold
    if indicator in {"bollinger_lower_touch", "bollinger_upper_touch"}:
        band = close.tail(window)
        average = _finite_float(band.mean())
        deviation = _finite_float(band.std(ddof=0))
        if average is None or deviation is None:
            return False
        lower = average - (2 * deviation)
        upper = average + (2 * deviation)
        return current <= lower if indicator == "bollinger_lower_touch" else current >= upper
    if indicator == "donchian_breakout":
        high = pd.to_numeric(frame["high"], errors="coerce") if "high" in frame.columns else close
        prior_high = _finite_float(high.iloc[-window:-1].max())
        return prior_high is not None and current > prior_high
    if indicator == "atr_breakout":
        if not {"high", "low"}.issubset(frame.columns):
            return False
        high = pd.to_numeric(frame["high"], errors="coerce")
        low = pd.to_numeric(frame["low"], errors="coerce")
        true_range = (high - low).tail(window)
        atr = _finite_float(true_range.mean())
        previous = _finite_float(close.iloc[-2])
        return atr is not None and previous is not None and current > previous + atr
    return False


def _condition_window(condition: Mapping[str, object]) -> int:
    try:
        value = _payload_int(condition.get("window"), 20)
    except ValueError:
        value = 20
    return max(2, min(value, 500))


def _condition_threshold(condition: Mapping[str, object], default: float) -> float:
    try:
        value = _payload_float(condition.get("threshold"), default)
    except ValueError:
        value = default
    return max(0, min(value, 100))


def _latest_rsi(close: pd.Series, window: int) -> float | None:
    delta = close.diff().dropna()
    if len(delta) < window:
        return None
    gains = delta.clip(lower=0).tail(window)
    losses = (-delta.clip(upper=0)).tail(window)
    average_loss = _finite_float(losses.mean())
    average_gain = _finite_float(gains.mean())
    if average_gain is None or average_loss is None:
        return None
    if average_loss == 0:
        return 100.0
    rs = average_gain / average_loss
    return 100 - (100 / (1 + rs))


def _submit_backtest_job(
    context: DashboardContext,
    payload: Mapping[str, object],
) -> JsonDict:
    errors = _validate_backtest_payload(context, payload)
    if errors:
        return {"ok": False, "errors": errors}
    queued = _queued_backtest_jobs(context)
    if queued >= _MAX_BACKTEST_QUEUE:
        return {"ok": False, "errors": [f"backtest queue is full: {queued} queued/running"]}
    spec = _normalized_backtest_spec(context, payload)
    job_id = uuid.uuid4().hex[:16]
    job = _new_backtest_job(job_id, spec)
    _write_backtest_job(context, job)
    thread = threading.Thread(
        target=_run_backtest_job,
        args=(context, job_id),
        daemon=True,
    )
    thread.start()
    return {"ok": True, "job": job}


def _validate_backtest_payload(
    context: DashboardContext,
    payload: Mapping[str, object],
) -> list[str]:
    raw_error = payload.get("_error")
    if raw_error:
        return [str(raw_error)]
    strategy_id = str(payload.get("strategy_id") or payload.get("strategy") or "").strip()
    if not strategy_id:
        return ["strategy_id is required"]
    registry = default_strategy_registry()
    known = set(registry.list())
    if strategy_id not in known:
        return [f"unknown strategy_id: {strategy_id}"]
    if not _is_spot_only(strategy_id):
        return [
            f"{strategy_id} requires derivative data; Phase 1 dashboard jobs run spot long/cash strategies only"
        ]
    ohlcv_key = str(payload.get("ohlcv_key") or _default_ohlcv_key(context)).strip()
    option = _option_for_key(_ohlcv_options(context), ohlcv_key)
    if option is None:
        return [f"unknown OHLCV key: {ohlcv_key}"]
    if _as_int(option.get("rows")) <= 0:
        return [f"unavailable OHLCV key: {ohlcv_key} has no rows"]
    try:
        initial_cash = _payload_float(payload.get("initial_cash"), 10_000)
    except (TypeError, ValueError):
        return ["initial_cash must be numeric"]
    if initial_cash <= 0:
        return ["initial_cash must be positive"]
    for name in ("fee_bps", "slippage_bps"):
        try:
            value = _payload_float(payload.get(name), 0)
        except (TypeError, ValueError):
            return [f"{name} must be numeric"]
        if value < 0:
            return [f"{name} must be non-negative"]
    if _recipe_has_conditions(payload.get("rules")):
        return [
            "built-in templates cannot be silently replaced by rules; use "
            "custom_rules mode with the custom_rule_recipe identity"
        ]
    return []


def _normalized_backtest_spec(
    context: DashboardContext,
    payload: Mapping[str, object],
) -> JsonDict:
    strategy_id = str(payload.get("strategy_id") or payload.get("strategy") or "sma_crossover")
    strategy_params = payload.get("strategy_params")
    if not isinstance(strategy_params, dict):
        strategy_params = {}
    return {
        "strategy_id": strategy_id,
        "strategy_params": strategy_params,
        "ohlcv_key": str(payload.get("ohlcv_key") or _default_ohlcv_key(context)),
        "initial_cash": _payload_float(payload.get("initial_cash"), 10_000),
        "fee_bps": _payload_float(payload.get("fee_bps"), 10),
        "slippage_bps": _payload_float(payload.get("slippage_bps"), 5),
        "seed": _payload_int(payload.get("seed"), 7),
        "market": "spot long/cash",
        "rules": payload.get("rules") if isinstance(payload.get("rules"), dict) else {},
    }


def _new_backtest_job(job_id: str, spec: Mapping[str, object]) -> JsonDict:
    created_at = datetime.now(timezone.utc).isoformat()
    return {
        "job_id": job_id,
        "status": "queued",
        "progress": 0,
        "created_at": created_at,
        "updated_at": created_at,
        "spec": dict(spec),
        "stages": [
            {"name": name, "status": "pending", "progress": 0}
            for name in _JOB_STAGE_NAMES
        ],
        "result": None,
        "error": None,
    }


def _queued_backtest_jobs(context: DashboardContext) -> int:
    jobs_dir = _jobs_dir(context)
    count = 0
    for path in jobs_dir.glob("*.json"):
        job = _read_json_file(path)
        if str(job.get("status")) in {"queued", "running"}:
            count += 1
    return count


def _run_backtest_job(context: DashboardContext, job_id: str) -> None:
    job = _read_backtest_job(context, job_id)
    if job is None:
        return
    try:
        _advance_job(context, job, "validation", 12, status="running")
        spec_raw = job.get("spec")
        spec = cast(Mapping[str, object], spec_raw) if isinstance(spec_raw, dict) else {}
        errors = _validate_backtest_payload(context, spec)
        if errors:
            raise ValueError(errors[0])
        _advance_job(context, job, "indicators", 28)
        _advance_job(context, job, "simulation", 48)
        summary = _run_research_backtest(context, spec)
        _advance_job(context, job, "metrics", 68)
        summary["scorecard"] = _research_scorecard(summary)
        _advance_job(context, job, "robustness", 82)
        summary["robustness"] = _lightweight_robustness(summary)
        _advance_job(context, job, "visualization", 94)
        _write_json_file(context.backtests_dir / "latest.json", summary)
        job["status"] = "complete"
        job["progress"] = 100
        job["result"] = summary
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        job["stages"] = _job_stages("complete")
        _write_backtest_job(context, job)
    except Exception as error:
        job["status"] = "failed"
        job["error"] = str(error)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_backtest_job(context, job)


def _advance_job(
    context: DashboardContext,
    job: JsonDict,
    stage: str,
    progress: int,
    *,
    status: str = "running",
) -> None:
    job["status"] = status
    job["progress"] = progress
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    job["stages"] = _job_stages(stage)
    _write_backtest_job(context, job)


def _job_stages(active: str) -> list[JsonDict]:
    stages: list[JsonDict] = []
    seen_active = False
    for name in _JOB_STAGE_NAMES:
        if name == active:
            seen_active = True
            stages.append({"name": name, "status": "complete" if name == "complete" else "running", "progress": 100 if name == "complete" else 50})
        elif not seen_active:
            stages.append({"name": name, "status": "complete", "progress": 100})
        else:
            stages.append({"name": name, "status": "pending", "progress": 0})
    return stages


def _run_research_backtest(
    context: DashboardContext,
    spec_payload: Mapping[str, object],
) -> JsonDict:
    strategy_id = str(spec_payload.get("strategy_id", "sma_crossover"))
    ohlcv_key = str(spec_payload.get("ohlcv_key", _default_ohlcv_key(context)))
    store = ParquetStore(context.parquet_dir)
    ohlcv = store.read("ohlcv", ohlcv_key)
    if ohlcv.empty:
        raise ValueError(f"no OHLCV rows for {ohlcv_key}")
    ohlcv = ohlcv.sort_index()
    timeframe = _infer_timeframe(ohlcv.index)
    request = _request_for(ohlcv, timeframe=timeframe, synthetic=False)
    spec = _spec(
        strategy_id,
        ohlcv,
        initial_cash=_payload_float(spec_payload.get("initial_cash"), 10_000),
        synthetic=False,
    ).model_copy(
        update={
            "strategy_params": spec_payload.get("strategy_params", {}),
            "fee_bps": Decimal(str(spec_payload.get("fee_bps", 10))),
            "slippage_bps": Decimal(str(spec_payload.get("slippage_bps", 5))),
            "seed": _payload_int(spec_payload.get("seed"), 7),
        }
    )
    result = EventRunner().run(
        spec,
        _market_dataset_bundle(ohlcv, request),
        _strategy_for_research_job(strategy_id, spec_payload, spec.strategy_params),
    )
    equity = _equity_series(result)
    trades = _trades_frame(result)
    metrics = compute_metrics(equity, _normalize_trades(trades))
    run_dir = _write_research_artifacts(
        context,
        strategy_id=strategy_id,
        ohlcv_key=ohlcv_key,
        spec=spec_payload,
        equity=equity,
        trades=trades,
        metrics=metrics,
        engine_run_id=result.run_id,
    )
    summary = _read_json_file(run_dir / "summary.json")
    return summary


def _strategy_for_research_job(
    strategy_id: str,
    spec_payload: Mapping[str, object],
    strategy_params: Mapping[str, object],
) -> object:
    rules = spec_payload.get("rules")
    if _recipe_has_conditions(rules):
        rule_map = cast(Mapping[str, object], rules)
        return _RuleRecipeStrategy(strategy_id, rule_map)
    return default_strategy_registry().create(strategy_id, strategy_params)


def _market_dataset_bundle(ohlcv: pd.DataFrame, request: DataRequest) -> MarketBundle:
    return MarketBundle(
        primary=_market_dataset(
            "spot",
            ohlcv,
            request=request,
            real_data=True,
            source="qt-dashboard://ohlcv",
        ),
        auxiliary={},
    )


def _write_research_artifacts(
    context: DashboardContext,
    *,
    strategy_id: str,
    ohlcv_key: str,
    spec: Mapping[str, object],
    equity: pd.Series,
    trades: pd.DataFrame,
    metrics: Metrics,
    engine_run_id: str,
) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = context.backtests_dir / f"research_{strategy_id}_{ts}_{uuid.uuid4().hex[:6]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    equity.to_frame("equity").to_csv(run_dir / "equity.csv", index_label="ts")
    trades.to_csv(run_dir / "trades.csv", index=False)
    summary = {
        "run_id": run_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy_id,
        "engine_strategy": strategy_id,
        "ohlcv_key": ohlcv_key,
        "initial_cash": spec.get("initial_cash", 10_000),
        "fee_bps": spec.get("fee_bps", 10),
        "slippage_bps": spec.get("slippage_bps", 5),
        "engine_run_id": engine_run_id,
        "market": "spot long/cash",
        "metrics": {
            "total_return": metrics.total_return,
            "cagr": metrics.cagr,
            "sharpe": metrics.sharpe,
            "sortino": metrics.sortino,
            "calmar": metrics.calmar,
            "max_drawdown": metrics.max_drawdown,
            "win_rate": metrics.win_rate,
            "profit_factor": metrics.profit_factor,
        },
        "counts": {
            "equity_points": len(equity),
            "trades": metrics.num_trades,
            "signals": 0,
        },
        "sources": {"ohlcv": ohlcv_key},
        "recipe": _recipe_summary(spec.get("rules")),
        "files": {
            "equity": str(run_dir / "equity.csv"),
            "trades": str(run_dir / "trades.csv"),
        },
    }
    _write_json_file(run_dir / "summary.json", summary)
    return run_dir


def _recipe_summary(raw: object) -> JsonDict | None:
    if not _recipe_has_conditions(raw) or not isinstance(raw, dict):
        return None
    conditions: list[str] = []
    entry = raw.get("entry")
    exit_rule = raw.get("exit")
    for group in (entry, exit_rule):
        if not isinstance(group, dict):
            continue
        raw_conditions = group.get("conditions")
        if not isinstance(raw_conditions, list):
            continue
        for condition in raw_conditions:
            if isinstance(condition, dict):
                conditions.append(str(condition.get("indicator", "")))
    return {
        "entry_operator": _rule_operator(entry),
        "exit_operator": _rule_operator(exit_rule),
        "conditions": [condition for condition in conditions if condition],
    }


def _rule_operator(raw: object) -> str:
    if not isinstance(raw, dict):
        return "ALL"
    operator = str(raw.get("operator", "ALL")).upper()
    return operator if operator in {"ALL", "ANY"} else "ALL"


def _research_scorecard(summary: Mapping[str, object]) -> JsonDict:
    metrics = summary.get("metrics")
    metrics_map = metrics if isinstance(metrics, dict) else {}
    counts = summary.get("counts")
    counts_map = counts if isinstance(counts, dict) else {}
    max_drawdown = _as_float(metrics_map.get("max_drawdown"))
    sharpe = _as_float(metrics_map.get("sharpe"))
    trades = _as_int(counts_map.get("trades"))
    data_score = 80 if str(summary.get("ohlcv_key", "")).startswith("bitstamp_") else 55
    drawdown_score = 90 if max_drawdown > -0.15 else 65 if max_drawdown > -0.35 else 35
    sample_score = 80 if trades >= 20 else 55 if trades >= 5 else 30
    quality_score = 80 if sharpe > 0.8 else 55 if sharpe > 0 else 30
    total = round((data_score + drawdown_score + sample_score + quality_score) / 4)
    return {
        "score": total,
        "data_quality": data_score,
        "drawdown": drawdown_score,
        "sample_size": sample_score,
        "risk_adjusted_quality": quality_score,
        "verdict": _research_verdict(total),
    }


def _research_verdict(score: int) -> str:
    if score >= 75:
        return "Research candidate: rerun on the 10-year standard dataset and paper trade before live use."
    if score >= 50:
        return "Educational result: useful for learning, not strong enough for automation."
    return "Reject for automation: change assumptions or strategy before paper trading."


def _lightweight_robustness(summary: Mapping[str, object]) -> JsonDict:
    metrics = summary.get("metrics")
    metrics_map = metrics if isinstance(metrics, dict) else {}
    return {
        "walk_forward": "planned standard gate: 3-year expanding train and 1-year tests",
        "monte_carlo": "planned standard gate: 500 deterministic resamples",
        "cost_stress": {
            "1x": _as_float(metrics_map.get("total_return")),
            "2x": None,
            "3x": None,
        },
        "note": "This first dashboard job stores the run and scorecard; full robustness gates remain package-level validation tasks.",
    }


def _jobs_dir(context: DashboardContext) -> Path:
    path = context.backtests_dir / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_backtest_job(context: DashboardContext, job: Mapping[str, object]) -> None:
    _write_json_file(_jobs_dir(context) / f"{job.get('job_id')}.json", job)


def _read_backtest_job(context: DashboardContext, job_id: str) -> JsonDict | None:
    safe = "".join(c for c in job_id if c.isalnum() or c in "-_")
    if not safe:
        return None
    path = _jobs_dir(context) / f"{safe}.json"
    if not path.exists():
        return None
    return _read_json_file(path)


def _research_run_summary(
    context: DashboardContext,
    run_id: str,
) -> JsonDict | None:
    safe = "".join(c for c in run_id if c.isalnum() or c in "-_")
    if not safe or safe != run_id:
        return None
    path = context.backtests_dir / safe / "summary.json"
    if not path.exists():
        return None
    return _read_json_file(path)


def _is_opaque_run_id(run_id: str) -> bool:
    return len(run_id) == 32 and all(
        character in "0123456789abcdef" for character in run_id
    )


def _v2_run_series(
    context: DashboardContext,
    summary: Mapping[str, object],
) -> JsonDict:
    configuration = summary.get("configuration")
    config = configuration if isinstance(configuration, dict) else {}
    key = str(config.get("ohlcv_key", ""))
    frame = ParquetStore(context.parquet_dir).read("ohlcv", key) if key else pd.DataFrame()
    run_id = str(summary.get("run_id", ""))
    run_dir = context.backtests_dir / run_id
    comparisons = _read_comparison_series(run_dir / "equity.csv")
    return {
        "candles": _candlestick_points(frame),
        "volume": _volume_points(frame),
        "equity": comparisons.get("strategy", []),
        "benchmark_equity": {
            "buy_and_hold": comparisons.get("buy_and_hold", []),
            "fixed_dca": comparisons.get("fixed_dca", []),
        },
        "drawdown": comparisons.get("drawdown", []),
        "markers": _read_trade_markers(str(run_dir / "trades.csv")),
    }


def _sources(context: DashboardContext) -> list[JsonDict]:
    store = ParquetStore(context.parquet_dir)
    return data_source_statuses(store)


def _latest_backtest(context: DashboardContext) -> JsonDict | None:
    return latest_backtest_summary(context.backtests_dir)


def _monitor(context: DashboardContext) -> JsonDict | None:
    snapshot = MonitorStateStore(context.monitor_state_path).read()
    return snapshot.as_dict() if snapshot else None


def _strategies(context: DashboardContext) -> list[JsonDict]:
    """Return a list of per-strategy state snapshots, sorted by name."""

    out: list[JsonDict] = []
    if not context.strategies_state_dir.exists():
        return out
    for path in sorted(context.strategies_state_dir.glob("*.json")):
        snap = MonitorStateStore(path).read()
        if snap is None:
            continue
        out.append(snap.as_dict())
    return out


def _strategy(context: DashboardContext, name: str) -> JsonDict | None:
    """Return one strategy state snapshot, or None if no state file exists."""

    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    if not safe:
        return None
    path = context.strategies_state_dir / f"{safe}.json"
    snap = MonitorStateStore(path).read()
    return snap.as_dict() if snap else None


def _intel(context: DashboardContext) -> JsonDict:
    """Return the latest ranked intelligence opportunities."""
    return read_opportunities(context.runtime_dir)


def _portfolios(context: DashboardContext) -> list[JsonDict]:
    """Return every strategy's portfolio snapshot."""
    return read_all_portfolios(context.runtime_dir)


def _portfolio(context: DashboardContext, name: str) -> JsonDict | None:
    """Return one strategy's portfolio snapshot, or None."""
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    if not safe:
        return None
    return read_portfolio(safe, context.runtime_dir)


def _backtest_options(context: DashboardContext) -> JsonDict:
    return {
        "strategies": list(_BACKTEST_STRATEGIES),
        "ohlcv_keys": _available_ohlcv_options(context),
        "defaults": {
            "strategy": "composite",
            "ohlcv_key": _default_ohlcv_key(context),
            "initial_cash": 100_000,
        },
    }


def _ohlcv_options(context: DashboardContext) -> list[JsonDict]:
    root = context.parquet_dir / "ohlcv"
    if not root.exists():
        return []
    options: list[JsonDict] = []
    for path in sorted(root.glob("*.parquet")):
        key = path.stem
        rows = 0
        start = ""
        end = ""
        try:
            frame = pd.read_parquet(path)
        except Exception:
            frame = pd.DataFrame()
        if not frame.empty:
            rows = len(frame)
            start = str(frame.index[0])
            end = str(frame.index[-1])
        options.append(
            {
                "key": key,
                "rows": rows,
                "start": start,
                "end": end,
                "path": str(path),
            }
        )
    return options


def _available_ohlcv_options(context: DashboardContext) -> list[JsonDict]:
    return [
        option for option in _ohlcv_options(context)
        if _as_int(option.get("rows")) > 0
    ]


def _default_ohlcv_key(context: DashboardContext) -> str:
    options = _available_ohlcv_options(context)
    if not options:
        return "okx_BTCUSDT_1h"
    for option in options:
        if option.get("key") == "okx_BTCUSDT_1h":
            return "okx_BTCUSDT_1h"
    first = options[0].get("key")
    return str(first) if first else "okx_BTCUSDT_1h"


def _run_backtest_request(
    context: DashboardContext,
    fields: Mapping[str, list[str]],
) -> JsonDict:
    mode = _form_value(fields, "mode", "single")
    strategy = _form_value(fields, "strategy", "composite")
    ohlcv_key = _form_value(fields, "ohlcv_key", _default_ohlcv_key(context))
    initial_cash_raw = _form_value(fields, "initial_cash", "100000")
    request = {
        "mode": mode,
        "strategy": strategy,
        "ohlcv_key": ohlcv_key,
        "initial_cash": initial_cash_raw,
    }
    if strategy not in _BACKTEST_STRATEGIES:
        return {"ok": False, "error": f"unknown strategy: {strategy}", "request": request}
    all_options = _ohlcv_options(context)
    all_keys = {str(item.get("key")) for item in all_options}
    available_keys = {
        str(item.get("key"))
        for item in all_options
        if _as_int(item.get("rows")) > 0
    }
    if ohlcv_key not in all_keys:
        return {"ok": False, "error": f"unknown OHLCV key: {ohlcv_key}", "request": request}
    if ohlcv_key not in available_keys:
        return {
            "ok": False,
            "error": (
                f"unavailable OHLCV key: {ohlcv_key} has no rows; "
                "choose a listed non-empty data source"
            ),
            "request": request,
        }
    try:
        initial_cash = float(initial_cash_raw)
    except ValueError:
        return {"ok": False, "error": "initial cash must be numeric", "request": request}
    if initial_cash <= 0:
        return {"ok": False, "error": "initial cash must be positive", "request": request}

    store = ParquetStore(context.parquet_dir)
    ohlcv = store.read("ohlcv", ohlcv_key)
    if ohlcv.empty:
        return {"ok": False, "error": f"no OHLCV rows for {ohlcv_key}", "request": request}

    try:
        if mode == "compare":
            summary = _run_backtest_comparison(
                context,
                store,
                ohlcv,
                ohlcv_key=ohlcv_key,
                initial_cash=initial_cash,
            )
        elif strategy == "composite":
            summary = _run_composite_backtest(
                context,
                store,
                ohlcv,
                ohlcv_key=ohlcv_key,
                initial_cash=initial_cash,
            )
        else:
            summary = _run_gallery_backtest(
                context,
                store,
                ohlcv,
                strategy=strategy,
                ohlcv_key=ohlcv_key,
                initial_cash=initial_cash,
            )
    except Exception as error:
        return {"ok": False, "error": str(error), "request": request}
    return {"ok": True, "summary": summary, "request": request}


def _run_backtest_comparison(
    context: DashboardContext,
    store: ParquetStore,
    ohlcv: pd.DataFrame,
    *,
    ohlcv_key: str,
    initial_cash: float,
) -> JsonDict:
    summaries = [
        _run_gallery_backtest(
            context,
            store,
            ohlcv,
            strategy=strategy,
            ohlcv_key=ohlcv_key,
            initial_cash=initial_cash,
        )
        for strategy in _COMPARE_STRATEGIES
    ]
    ranked = sorted(summaries, key=_strategy_score, reverse=True)
    best = ranked[0] if ranked else {}
    comparison = {
        "mode": "compare",
        "strategy": str(best.get("strategy", "")),
        "ohlcv_key": ohlcv_key,
        "initial_cash": initial_cash,
        "ranked": [_comparison_row(summary, rank=index + 1) for index, summary in enumerate(ranked)],
        "recommended": _comparison_recommendation(best),
        "best_run_id": str(best.get("run_id", "")),
    }
    _write_json_file(context.backtests_dir / "latest_comparison.json", comparison)
    best["comparison"] = comparison
    _write_json_file(context.backtests_dir / "latest.json", best)
    return best


def _run_composite_backtest(
    context: DashboardContext,
    store: ParquetStore,
    ohlcv: pd.DataFrame,
    *,
    ohlcv_key: str,
    initial_cash: float,
) -> JsonDict:
    settings = load_settings()
    backtester = Backtester(
        thresholds=settings.thresholds,
        risk_cfg=settings.risk,
        initial_cash=initial_cash,
    )
    result = backtester.run(
        ohlcv=ohlcv,
        funding=_series(store, "derivatives", "binance_BTCUSDT_funding", "funding_rate"),
        oi=_series(store, "derivatives", "binance_BTCUSDT_oi_1h", "oi_usd"),
        long_short_ratio=_series(
            store,
            "derivatives",
            "binance_BTCUSDT_lsr_1h",
            "long_short_ratio",
        ),
        sopr=_series(store, "onchain", "glassnode_sopr_adj", "sopr_adj"),
        mvrv_z=_series(store, "onchain", "glassnode_mvrv_z", "mvrv_z"),
        nupl=_series(store, "onchain", "glassnode_nupl", "nupl"),
        puell=_series(store, "onchain", "glassnode_puell_multiple", "puell_multiple"),
        reserve_risk=_series(
            store,
            "onchain",
            "glassnode_reserve_risk",
            "reserve_risk",
        ),
        exchange_netflow=_series(
            store,
            "onchain",
            "glassnode_exchange_netflow",
            "exchange_netflow",
        ),
        fear_greed=_series(store, "sentiment", "fear_greed", "fear_greed"),
        social_sentiment=_series(
            store,
            "sentiment",
            "santiment_sentiment_weighted_total_btc",
            "sentiment_weighted_total_btc",
        ),
        vix=_series(store, "macro", "fred_vix", "vix"),
        dxy=_series(store, "macro", "fred_dxy", "dxy"),
    )
    artifact = write_backtest_artifacts(
        result,
        context.backtests_dir,
        ohlcv_key=ohlcv_key,
        initial_cash=initial_cash,
        data_fingerprint=ohlcv_fingerprint(ohlcv),
        sources={
            "ohlcv": ohlcv_key,
            "funding": "binance_BTCUSDT_funding",
            "oi": "binance_BTCUSDT_oi_1h",
            "long_short_ratio": "binance_BTCUSDT_lsr_1h",
            "fear_greed": "fear_greed",
        },
    )
    return _read_json_file(artifact.summary_path)


def _run_gallery_backtest(
    context: DashboardContext,
    store: ParquetStore,
    ohlcv: pd.DataFrame,
    *,
    strategy: str,
    ohlcv_key: str,
    initial_cash: float,
) -> JsonDict:
    outcome = run_strategy_backtest(
        strategy,
        ohlcv,
        initial_cash=initial_cash,
        funding=_series(store, "derivatives", "binance_BTCUSDT_funding", "funding_rate"),
        fear_greed=_series(store, "sentiment", "fear_greed", "fear_greed"),
        mvrv_z=_series(store, "onchain", "glassnode_mvrv_z", "mvrv_z"),
        nupl=_series(store, "onchain", "glassnode_nupl", "nupl"),
        allow_synthetic=False,
    )
    run_dir = write_strategy_backtest_artifacts(outcome, context.backtests_dir)
    summary = _read_json_file(run_dir / "summary.json")
    summary["ohlcv_key"] = ohlcv_key
    summary["initial_cash"] = initial_cash
    summary["counts"] = {
        "equity_points": _as_int(summary.get("bars")),
        "trades": _as_int(summary.get("num_trades")),
        "signals": 0,
    }
    summary["sources"] = {"ohlcv": ohlcv_key}
    _write_json_file(context.backtests_dir / "latest.json", summary)
    return summary


def _strategy_score(summary: Mapping[str, object]) -> float:
    metrics = summary.get("metrics")
    metrics_map = metrics if isinstance(metrics, dict) else {}
    total_return = _as_float(metrics_map.get("total_return"))
    sharpe = _as_float(metrics_map.get("sharpe"))
    max_drawdown = _as_float(metrics_map.get("max_drawdown"))
    trades = _as_int(summary.get("num_trades"))
    trade_penalty = 0.35 if trades <= 0 else 0.0
    return (total_return * 1.5) + (sharpe * 0.25) + max_drawdown - trade_penalty


def _comparison_row(summary: Mapping[str, object], *, rank: int) -> JsonDict:
    metrics = summary.get("metrics")
    metrics_map = metrics if isinstance(metrics, dict) else {}
    trades = _as_int(summary.get("num_trades"))
    return {
        "rank": rank,
        "strategy": str(summary.get("strategy") or summary.get("engine_strategy") or ""),
        "run_id": str(summary.get("run_id", "")),
        "total_return": _as_float(metrics_map.get("total_return")),
        "max_drawdown": _as_float(metrics_map.get("max_drawdown")),
        "sharpe": _as_float(metrics_map.get("sharpe")),
        "trades": trades,
        "score": _strategy_score(summary),
        "verdict": _prediction_sentence(
            _as_float(metrics_map.get("total_return")),
            _as_float(metrics_map.get("sharpe")),
            trades,
        ),
    }


def _comparison_recommendation(summary: Mapping[str, object]) -> str:
    metrics = summary.get("metrics")
    metrics_map = metrics if isinstance(metrics, dict) else {}
    strategy = str(summary.get("strategy") or summary.get("engine_strategy") or "unknown")
    total_return = _as_float(metrics_map.get("total_return"))
    max_drawdown = _as_float(metrics_map.get("max_drawdown"))
    sharpe = _as_float(metrics_map.get("sharpe"))
    trades = _as_int(summary.get("num_trades"))
    if trades <= 0:
        return (
            f"{strategy} ranked first but made no trades; treat this as no candidate "
            "until a longer or different data window is tested."
        )
    if total_return > 0 and sharpe > 0 and max_drawdown > -0.35:
        return f"Recommended first paper candidate: {strategy}. Paper trade before live use."
    return (
        f"{strategy} ranked first, but quality is not production-ready. "
        "Use it for research, then rerun with more data and paper trading."
    )


def _series(
    store: ParquetStore,
    dataset: str,
    key: str,
    column: str | None = None,
) -> pd.Series | pd.DataFrame | None:
    frame = store.read(dataset, key)
    if frame.empty:
        return None
    if column and column in frame.columns:
        return frame[column]
    if frame.shape[1] == 1:
        return frame.iloc[:, 0]
    return frame


def _form_value(fields: Mapping[str, list[str]], name: str, default: str) -> str:
    values = fields.get(name)
    if not values:
        return default
    value = values[0].strip()
    return value or default


def _read_json_file(path: Path) -> JsonDict:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _write_json_file(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    temporary.replace(path)


def _backtest_chart_payload(
    context: DashboardContext,
    summary: Mapping[str, object] | None,
) -> JsonDict:
    if summary is None:
        return {"candles": [], "equity": [], "markers": []}
    if str(summary.get("schema_version", "")) == "2":
        return _v2_run_series(context, summary)
    ohlcv_key = str(summary.get("ohlcv_key") or "")
    store = ParquetStore(context.parquet_dir)
    ohlcv = store.read("ohlcv", ohlcv_key) if ohlcv_key else pd.DataFrame()
    files = summary.get("files")
    file_map = files if isinstance(files, dict) else {}
    equity = _read_equity_points(file_map.get("equity"))
    markers = _read_trade_markers(file_map.get("trades"))
    return {
        "ohlcv_key": ohlcv_key,
        "candles": _candlestick_points(ohlcv),
        "equity": equity,
        "markers": markers,
    }


def _candlestick_points(frame: pd.DataFrame) -> list[JsonDict]:
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return []
    trimmed = _sample_frame(frame)
    points: list[JsonDict] = []
    for ts, row in trimmed.iterrows():
        point: JsonDict = {
            "time": _unix_time(ts),
            "open": _finite_float(row.get("open")),
            "high": _finite_float(row.get("high")),
            "low": _finite_float(row.get("low")),
            "close": _finite_float(row.get("close")),
        }
        if all(value is not None for key, value in point.items() if key != "time"):
            points.append(point)
    return points


def _volume_points(frame: pd.DataFrame) -> list[JsonDict]:
    if (
        frame.empty
        or not isinstance(frame.index, pd.DatetimeIndex)
        or "volume" not in frame.columns
    ):
        return []
    trimmed = _sample_frame(frame)
    points: list[JsonDict] = []
    for ts, row in trimmed.iterrows():
        value = _finite_float(row.get("volume"))
        close = _finite_float(row.get("close"))
        open_price = _finite_float(row.get("open"))
        if value is None or close is None or open_price is None:
            continue
        points.append(
            {
                "time": _unix_time(ts),
                "value": value,
                "color": "#38d6c855" if close >= open_price else "#ff6b6b55",
            }
        )
    return points


def _read_comparison_series(path: Path) -> dict[str, list[JsonDict]]:
    if not path.is_file():
        return {}
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError):
        return {}
    if frame.empty or "timestamp" not in frame.columns:
        return {}
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"], utc=True, errors="coerce"
    )
    frame = frame.dropna(subset=["timestamp"]).set_index("timestamp")
    frame = _sample_frame(frame)
    output: dict[str, list[JsonDict]] = {}
    for column in ("strategy", "buy_and_hold", "fixed_dca"):
        if column not in frame.columns:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        output[column] = [
            {"time": _unix_time(ts), "value": value}
            for ts, raw in values.items()
            if (value := _finite_float(raw)) is not None
        ]
    if "strategy" in frame.columns:
        values = pd.to_numeric(frame["strategy"], errors="coerce")
        drawdown = values / values.cummax() - 1
        output["drawdown"] = [
            {"time": _unix_time(ts), "value": value}
            for ts, raw in drawdown.items()
            if (value := _finite_float(raw)) is not None
        ]
    return output


def _read_equity_points(raw_path: object) -> list[JsonDict]:
    path = _artifact_path(raw_path)
    if path is None or not path.exists():
        return []
    try:
        frame = pd.read_csv(path)
    except Exception:
        return []
    if frame.empty:
        return []
    ts_col = "ts" if "ts" in frame.columns else str(frame.columns[0])
    value_col = "equity" if "equity" in frame.columns else str(frame.columns[-1])
    frame[ts_col] = pd.to_datetime(frame[ts_col], utc=True, errors="coerce")
    frame[value_col] = pd.to_numeric(frame[value_col], errors="coerce")
    frame = frame.dropna(subset=[ts_col, value_col])
    if frame.empty:
        return []
    frame = _sample_frame(frame.set_index(ts_col))
    return [
        {"time": _unix_time(ts), "value": _finite_float(row[value_col])}
        for ts, row in frame.iterrows()
        if _finite_float(row[value_col]) is not None
    ]


def _read_trade_markers(raw_path: object) -> list[JsonDict]:
    path = _artifact_path(raw_path)
    if path is None or not path.exists():
        return []
    try:
        frame = pd.read_csv(path)
    except Exception:
        return []
    if frame.empty:
        return []
    ts_col = _first_column(frame, ("ts", "timestamp", "closed_at", "opened_at", "time"))
    side_col = _first_column(frame, ("side", "action", "type"))
    if ts_col is None:
        return []
    frame[ts_col] = pd.to_datetime(frame[ts_col], utc=True, errors="coerce")
    frame = frame.dropna(subset=[ts_col])
    markers: list[JsonDict] = []
    for _, row in frame.iterrows():
        side = str(row.get(side_col, "trade") if side_col else "trade").lower()
        is_buy = side in {"buy", "long", "open"}
        markers.append(
            {
                "time": _unix_time(row[ts_col]),
                "position": "belowBar" if is_buy else "aboveBar",
                "color": "#1f9d6a" if is_buy else "#d05a3b",
                "shape": "arrowUp" if is_buy else "arrowDown",
                "text": "Buy" if is_buy else "Sell",
            }
        )
    return markers[:200]


def _artifact_path(raw_path: object) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    return Path(raw_path)


def _first_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    normalized = {str(column).lower(): str(column) for column in frame.columns}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    return None


def _sample_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if len(frame) <= _CHART_MAX_POINTS:
        return frame
    step = max(1, math.ceil(len(frame) / _CHART_MAX_POINTS))
    return frame.iloc[::step]


def _unix_time(value: object) -> int:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp())


def _finite_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value if isinstance(value, int | float | str) else str(value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _payload_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("boolean is not numeric")
    if isinstance(value, int | float | str):
        number = float(value)
        if math.isfinite(number):
            return number
    raise ValueError("value must be numeric")


def _payload_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer")
    if isinstance(value, int | str):
        return int(value)
    raise ValueError("value must be an integer")


def _json_script(payload: Mapping[str, object]) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"), default=str)
    return (
        payload_json
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _plain_summary(
    strategies: Sequence[Mapping[str, object]],
    portfolios: Sequence[Mapping[str, object]],
) -> tuple[str, str, str]:
    """Return (traffic_light_class, one_line_en, one_line_zh) in plain language."""
    running = [s for s in strategies if str(s.get("status")) in {"healthy", "starting"}]
    failed = [s for s in strategies if str(s.get("status")) in {"failed"}]
    degraded = [s for s in strategies if str(s.get("status")) == "degraded"]

    total_realized = sum((_as_float(p.get("realized_pnl")) for p in portfolios), 0.0)
    total_trades = sum((_as_int(p.get("num_trades")) for p in portfolios), 0)

    if failed:
        light = "bad"
    elif degraded:
        light = "warn"
    elif running:
        light = "good"
    else:
        light = "muted"

    pnl_word = "up" if total_realized > 0 else "down" if total_realized < 0 else "flat"
    pnl_word_zh = "盈利" if total_realized > 0 else "亏损" if total_realized < 0 else "持平"
    en = (
        f"{len(running)} strategy(ies) running, {len(failed)} failed. "
        f"So far {total_trades} paper trade(s); realized P&L is {pnl_word} "
        f"{abs(total_realized):,.2f} USDT."
    )
    zh = (
        f"{len(running)} 个策略在运行，{len(failed)} 个故障。"
        f"目前共 {total_trades} 笔纸面交易；已实现盈亏{pnl_word_zh} "
        f"{abs(total_realized):,.2f} USDT。"
    )
    return light, en, zh


def _render_plain_banner(strategies: list[JsonDict], portfolios: list[JsonDict]) -> str:
    light, en, zh = _plain_summary(strategies, portfolios)
    dot = {"good": "&#128994;", "warn": "&#128993;", "bad": "&#128308;", "muted": "&#9899;"}[light]
    return (
        '<div class="panel" style="border-left:4px solid var(--line)">'
        f'<div style="font-size:15px"><strong>{dot} This week / 本周</strong></div>'
        f'<div style="margin-top:6px">{_e(en)}</div>'
        f'<div class="subtle" style="margin-top:4px">{_e(zh)}</div>'
        '<div class="subtle" style="margin-top:8px">'
        'Paper mode is safe — no real money moves until you follow '
        '<span class="mono">docs/live-checklist.md</span>. '
        '纸面模式安全，未接真钱。</div>'
        '</div>'
    )


def _render_home(context: DashboardContext) -> str:
    sources = _sources(context)
    backtest = _latest_backtest(context)
    monitor = _monitor(context)
    strategies = _strategies(context)
    portfolios = _portfolios(context)
    intel = _intel(context)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>QT Monitor</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f3;
      --ink: #15201b;
      --muted: #66736c;
      --line: #dce2dd;
      --accent: #0b7a75;
      --warn: #ad5a00;
      --bad: #a73737;
      --good: #197447;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); }}
    main {{ width: min(1180px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 48px; }}
    header {{ display: flex; justify-content: space-between; gap: 20px; align-items: flex-end; margin-bottom: 22px; }}
    h1 {{ font-size: 28px; line-height: 1.1; margin: 0; letter-spacing: 0; }}
    h2 {{ font-size: 18px; margin: 28px 0 12px; letter-spacing: 0; }}
    .subtle {{ color: var(--muted); font-size: 14px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    .panel {{ border: 1px solid var(--line); background: #fff; border-radius: 8px; padding: 14px; }}
    .metric {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px; text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ color: var(--muted); font-weight: 700; background: #fbfcfa; }}
    tr:last-child td {{ border-bottom: 0; }}
    .pill {{ display: inline-flex; border-radius: 999px; padding: 2px 8px; font-weight: 700; font-size: 12px; }}
    .good {{ color: var(--good); background: #e8f3ed; }}
    .warn {{ color: var(--warn); background: #fff0dd; }}
    .bad {{ color: var(--bad); background: #f8e7e7; }}
    .muted {{ color: var(--muted); background: #eef1ee; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    @media (max-width: 900px) {{
      header {{ display: block; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      table {{ display: block; overflow-x: auto; }}
    }}
    @media (max-width: 560px) {{
      main {{ width: min(100vw - 20px, 1180px); padding-top: 18px; }}
      .grid {{ grid-template-columns: 1fr; }}
      th, td {{ white-space: nowrap; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>QT Monitor</h1>
        <div class="subtle">Data coverage, live heartbeat, P&amp;L, and latest backtest artifacts.</div>
      </div>
      <div class="subtle mono"><a href="/platform">Platform</a> · <a href="/backtest">Backtest</a> · <a href="/learn">Learn</a> · <a href="/demo">Demo</a> · <a href="/intel">Intel</a> · <a href="/portfolio">P&amp;L →</a> · refreshes every 60s</div>
    </header>
    {_render_plain_banner(strategies, portfolios)}
    {_render_monitor_cards(monitor)}
    <h2>Intelligence</h2>
    {_render_intel_summary(intel)}
    <h2>Portfolio P&amp;L</h2>
    {_render_portfolio_summary(portfolios)}
    <h2>Strategies</h2>
    {_render_strategies_table(strategies)}
    <h2>Latest Backtest</h2>
    {_render_backtest(backtest)}
    <h2>Data Sources</h2>
    {_render_sources_table(sources)}
  </main>
</body>
</html>
"""


def _render_monitor_cards(monitor: JsonDict | None) -> str:
    if monitor is None:
        return '<div class="panel subtle">No monitor heartbeat yet.</div>'
    status = str(monitor.get("status", "unknown"))
    cls = "good" if status == "healthy" else "warn" if status == "degraded" else "bad"
    details = monitor.get("details")
    detail_text = json.dumps(details, sort_keys=True, default=str) if isinstance(details, dict) else "{}"
    return f"""
    <div class="grid">
      {_card("Status", f'<span class="pill {cls}">{_e(status)}</span>')}
      {_card("Cycle", _e(str(monitor.get("cycle", 0))))}
      {_card("Updated", _e(str(monitor.get("updated_at", ""))))}
      {_card("Next Run", _e(str(monitor.get("next_run_at", ""))))}
    </div>
    <div class="panel" style="margin-top:12px">
      <div class="subtle">Last error</div>
      <div class="mono">{_e(str(monitor.get("last_error") or "none"))}</div>
      <div class="subtle" style="margin-top:10px">Last cycle details</div>
      <div class="mono">{_e(detail_text)}</div>
    </div>
    """


def _render_backtest(backtest: JsonDict | None) -> str:
    if backtest is None:
        return (
            '<div class="panel subtle">No exported backtest yet. '
            '<a href="/backtest">Run one from /backtest</a>.</div>'
        )
    metrics = backtest.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    counts = backtest.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    return f"""
    <div class="grid">
      {_card("Total Return", _fmt_pct(metrics.get("total_return")))}
      {_card("Sharpe", _fmt_num(metrics.get("sharpe")))}
      {_card("Max Drawdown", _fmt_pct(metrics.get("max_drawdown")))}
      {_card("Trades", _e(str(counts.get("trades", 0))))}
    </div>
    <table style="margin-top:12px">
      <tbody>
        <tr><th>Run</th><td class="mono">{_e(str(backtest.get("run_id", "")))}</td></tr>
        <tr><th>Created</th><td>{_e(str(backtest.get("created_at", "")))}</td></tr>
        <tr><th>OHLCV Key</th><td class="mono">{_e(str(backtest.get("ohlcv_key", "")))}</td></tr>
        <tr><th>Files</th><td class="mono">{_e(json.dumps(backtest.get("files", {}), sort_keys=True, default=str))}</td></tr>
      </tbody>
    </table>
    """


def _render_backtest_page(
    context: DashboardContext,
    result: JsonDict | None = None,
) -> str:
    all_options = _ohlcv_options(context)
    options = _available_ohlcv_options(context)
    latest = _latest_backtest(context)
    chart_summary = _summary_from_result(result) or latest
    chart_payload = _backtest_chart_payload(context, chart_summary)
    request = _request_from_result(result)
    selected_strategy = str(request.get("strategy", "composite"))
    selected_key = str(request.get("ohlcv_key", _default_ohlcv_key(context)))
    if selected_key not in {str(option.get("key")) for option in options}:
        selected_key = _default_ohlcv_key(context)
    initial_cash_value = str(request.get("initial_cash", "100000"))
    result_html = _render_backtest_result(result)
    strategy_options = "".join(
        f'<option value="{_e(strategy)}"'
        f'{" selected" if strategy == selected_strategy else ""}>'
        f'{_e(_strategy_label(strategy))}</option>'
        for strategy in _BACKTEST_STRATEGIES
    )
    data_options = "".join(
        f'<option value="{_e(str(option.get("key", "")))}"'
        f'{" selected" if option.get("key") == selected_key else ""}>'
        f'{_e(str(option.get("key", "")))} · {option.get("rows", 0)} rows'
        "</option>"
        for option in options
    )
    can_run = bool(data_options)
    if not data_options:
        data_options = '<option value="">No non-empty OHLCV files found</option>'
    submit_attrs = "" if can_run else " disabled"
    selected_option = _option_for_key(options, selected_key)
    strategy_card = _strategy_explainer_card(selected_strategy)
    source_card = _data_source_card(selected_option)
    latest_explanation = _result_explainer(chart_summary)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QT — Backtest</title>
  <style>
    :root {{
      color-scheme: light;
      --bg:#f4f2ea; --ink:#172019; --muted:#66726b; --panel:#fffdf7;
      --line:#ded8ca; --accent:#0b746b; --amber:#c27a15; --bad:#a93d34;
      --good:#166c4f; --slate:#18221e; --tape:#101715;
      font-family: "Aptos", Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:radial-gradient(circle at top left, #fffaf0 0, var(--bg) 36%, #ece7dc 100%); color:var(--ink); }}
    main {{ width:min(1180px, calc(100vw - 32px)); margin:0 auto; padding:30px 0 56px; }}
    header {{ display:grid; grid-template-columns:1.2fr .8fr; gap:22px; align-items:end; margin-bottom:18px; }}
    h1 {{ font-size:clamp(34px, 5vw, 62px); line-height:.92; margin:0; letter-spacing:-0.055em; max-width:760px; }}
    h2 {{ font-size:20px; margin:30px 0 12px; letter-spacing:-0.02em; }}
    h3 {{ font-size:15px; margin:0 0 8px; }}
    a {{ color:var(--accent); }}
    form {{ display:grid; grid-template-columns:1.05fr 1.35fr .8fr auto; gap:12px; align-items:end; }}
    label {{ display:grid; gap:7px; color:var(--muted); font-size:13px; font-weight:800; }}
    select, input {{ font:inherit; color:var(--ink); background:#fff; border:1px solid var(--line); border-radius:12px; padding:11px 12px; }}
    button {{ font:inherit; font-weight:850; color:#fff; background:var(--accent); border:0; border-radius:12px; padding:12px 16px; cursor:pointer; box-shadow:0 10px 24px rgba(11,116,107,.18); }}
    button.secondary {{ color:var(--accent); background:#e8f3ef; box-shadow:none; }}
    button:disabled {{ background:#9aa6a1; cursor:not-allowed; box-shadow:none; }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); border-radius:14px; overflow:hidden; }}
    th, td {{ border-bottom:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; font-size:13px; }}
    th {{ color:var(--muted); font-weight:800; background:#fbf7ed; }}
    tr:last-child td {{ border-bottom:0; }}
    .subtle {{ color:var(--muted); font-size:14px; }}
    .eyebrow {{ color:var(--amber); font-weight:900; text-transform:uppercase; letter-spacing:.12em; font-size:12px; }}
    .panel {{ border:1px solid var(--line); background:rgba(255,253,247,.92); border-radius:18px; padding:16px; }}
    .hero-note {{ border-left:4px solid var(--accent); padding:12px 14px; background:#fffdf7; border-radius:12px; }}
    .grid {{ display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; }}
    .two {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
    .steps {{ display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:12px; margin:18px 0; }}
    .step {{ border:1px solid var(--line); border-radius:18px; padding:15px; background:#fffdf7; }}
    .step .num {{ display:inline-grid; place-items:center; width:28px; height:28px; border-radius:50%; background:var(--tape); color:#fff; font-weight:900; margin-bottom:10px; }}
    .metric {{ font-size:26px; font-weight:900; margin-top:7px; letter-spacing:-.03em; }}
    .pill {{ display:inline-flex; border-radius:999px; padding:3px 9px; font-weight:850; font-size:12px; }}
    .good {{ color:var(--good); background:#e5f3ed; }}
    .warn {{ color:#805000; background:#fff1cf; }}
    .bad {{ color:var(--bad); background:#f9e5e1; }}
    .muted {{ color:var(--muted); background:#edf0ed; }}
    .mono {{ font-family:"SF Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .recorder {{ background:linear-gradient(135deg, #121916, #26322c); color:#f4f7ef; border:0; box-shadow:0 18px 48px rgba(18,25,22,.24); }}
    .recorder .subtle, .recorder th {{ color:#b8c6bd; }}
    .recorder table {{ background:#151d19; border-color:#38443d; }}
    .recorder th, .recorder td {{ border-color:#38443d; background:transparent; }}
    .chart-shell {{ min-height:420px; border:1px solid var(--line); border-radius:18px; background:#fff; padding:10px; }}
    #price-chart {{ min-height:390px; }}
    .progress {{ height:8px; border-radius:999px; background:#e2ddd1; overflow:hidden; margin-top:10px; }}
    .progress > div {{ height:100%; width:0; background:linear-gradient(90deg, var(--accent), var(--amber)); transition:width .35s ease; }}
    .run-status {{ margin-top:10px; font-weight:800; }}
    .actions {{ display:grid; gap:8px; }}
    .learning-list {{ display:grid; gap:8px; padding-left:20px; }}
    @media (max-width: 900px) {{
      header, form, .grid, .two, .steps {{ grid-template-columns:1fr; }}
      table {{ display:block; overflow-x:auto; }}
    }}
    @media (prefers-reduced-motion: reduce) {{ .progress > div {{ transition:none; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <div class="eyebrow">BTC strategy replay</div>
        <h1>Backtest without guessing what the button did.</h1>
      </div>
      <div class="hero-note">
        <strong>Safe mode:</strong> this page replays historical local data only.
        No exchange key is used and no real order is sent.
        <div class="subtle mono" style="margin-top:8px"><a href="/">← monitor</a> · <a href="/api/backtests/latest">latest JSON</a></div>
      </div>
    </header>
    <section class="steps" aria-label="Backtest workflow">
      <div class="step"><div class="num">1</div><h3>Step 1 · Choose what to test</h3><div class="subtle">Pick the strategy, local OHLCV data, and starting cash. The page explains each control before you run.</div></div>
      <div class="step"><div class="num">2</div><h3>Step 2 · Run the replay</h3><div class="subtle">The server replays every historical candle, applies the algorithm, writes artifacts, then reloads the result.</div></div>
      <div class="step"><div class="num">3</div><h3>Step 3 · Read the result</h3><div class="subtle">Check return, drawdown, trade count, buy/sell markers, and whether the risk is acceptable before paper trading.</div></div>
    </section>
    <section class="panel">
      <h2 style="margin-top:0">Configure the replay</h2>
      <form method="post" action="/backtest/run">
        <label>Strategy
          <select name="strategy">{strategy_options}</select>
        </label>
        <label>Data source
          <select name="ohlcv_key">{data_options}</select>
        </label>
        <label>Initial cash
          <input name="initial_cash" value="{_e(initial_cash_value)}" inputmode="decimal">
        </label>
        <div class="actions">
          <button type="submit" name="mode" value="single"{submit_attrs}>Run backtest</button>
          <button class="secondary" type="submit" name="mode" value="compare"{submit_attrs}>Auto compare strategies</button>
        </div>
      </form>
      <div id="run-status" class="run-status" aria-live="polite">Ready. Select a strategy and press Run backtest.</div>
      <div class="progress" aria-hidden="true"><div id="run-progress"></div></div>
      <div class="subtle" style="margin-top:10px"><strong>Auto mode:</strong> Let QT test DCA, trend, carry, and wick on the same data and cash, then rank them by return, drawdown, Sharpe, and trade sample.</div>
      <div class="two" style="margin-top:14px">{strategy_card}{source_card}</div>
    </section>
    <h2>Configuration guide</h2>
    {_render_backtest_config_guide()}
    <h2>Algorithm guide</h2>
    {_render_strategy_guide_table()}
    {result_html}
    <h2>Backtest flight recorder</h2>
    <section class="panel recorder">
      {latest_explanation}
    </section>
    <h2>Price curve with Buy/sell markers</h2>
    <section class="chart-shell">
      <div id="price-chart" role="img" aria-label="Candlestick price chart with equity curve and trade markers"></div>
      <div class="subtle" style="margin-top:8px">
        Chart powered by TradingView Lightweight Charts
        (<a href="https://www.tradingview.com/" rel="noopener">TradingView</a>).
        If the chart script is blocked, use the tables below.
      </div>
    </section>
    <h2>Latest Published Result</h2>
    {_render_backtest(latest)}
    <h2>What to learn before automation</h2>
    {_render_learning_before_automation()}
    <h2>Available OHLCV Data</h2>
    {_render_ohlcv_table(all_options)}
    <script type="application/json" id="backtest-chart-data">{_json_script(chart_payload)}</script>
    <script src="/static/lightweight-charts-5.2.0.js"></script>
    <script>
      (function () {{
        var form = document.querySelector('form[action="/backtest/run"]');
        var status = document.getElementById('run-status');
        var progress = document.getElementById('run-progress');
        if (form && status && progress) {{
          form.addEventListener('submit', function (event) {{
            var submitter = event.submitter;
            var isCompare = submitter && submitter.value === 'compare';
            status.textContent = isCompare
              ? 'Running auto comparison: testing DCA, trend, carry, and wick on the same data...'
              : 'Running: loading local candles, applying the strategy, and writing result artifacts...';
            progress.style.width = '35%';
            var buttons = form.querySelectorAll('button[type="submit"]');
            buttons.forEach(function (button) {{
              button.disabled = true;
              if (button === submitter) button.textContent = isCompare ? 'Comparing...' : 'Running...';
            }});
            window.setTimeout(function () {{ progress.style.width = '68%'; }}, 700);
            window.setTimeout(function () {{ progress.style.width = '88%'; status.textContent = 'Still running: large datasets can take a few seconds.'; }}, 1800);
          }});
        }}
        var chartNode = document.getElementById('price-chart');
        var dataNode = document.getElementById('backtest-chart-data');
        if (!chartNode || !dataNode || !window.LightweightCharts) {{
          if (chartNode) chartNode.innerHTML = '<div class="subtle" style="padding:18px">Chart library unavailable. The result tables remain valid.</div>';
          return;
        }}
        var payload = JSON.parse(dataNode.textContent || '{{}}');
        if (!payload.candles || payload.candles.length === 0) {{
          chartNode.innerHTML = '<div class="subtle" style="padding:18px">Run a backtest to draw price, equity, and trade markers.</div>';
          return;
        }}
        var chart = LightweightCharts.createChart(chartNode, {{
          height: 390,
          layout: {{ background: {{ type: 'solid', color: '#ffffff' }}, textColor: '#28332d' }},
          grid: {{ vertLines: {{ color: '#eee7db' }}, horzLines: {{ color: '#eee7db' }} }},
          rightPriceScale: {{ borderColor: '#ded8ca' }},
          timeScale: {{ borderColor: '#ded8ca' }}
        }});
        var candles = chart.addSeries(LightweightCharts.CandlestickSeries, {{
          upColor: '#1f9d6a', downColor: '#d05a3b', borderVisible: false,
          wickUpColor: '#1f9d6a', wickDownColor: '#d05a3b'
        }});
        candles.setData(payload.candles);
        if (payload.equity && payload.equity.length) {{
          var equity = chart.addSeries(LightweightCharts.LineSeries, {{
            color: '#0b746b', lineWidth: 2, priceScaleId: 'left'
          }});
          equity.setData(payload.equity);
        }}
        if (payload.markers && payload.markers.length) {{
          if (LightweightCharts.createSeriesMarkers) {{
            LightweightCharts.createSeriesMarkers(candles, payload.markers);
          }} else if (candles.setMarkers) {{
            candles.setMarkers(payload.markers);
          }}
        }}
        chart.timeScale().fitContent();
        window.addEventListener('resize', function () {{
          chart.applyOptions({{ width: chartNode.clientWidth }});
        }});
      }})();
    </script>
  </main>
</body>
</html>
"""


def _render_backtest_builder_page(context: DashboardContext) -> str:
    catalog = _v2_backtest_catalog(context)
    strategies = catalog.get("strategies")
    strategy_client_catalog: dict[str, JsonDict] = {}
    strategy_options = ""
    if isinstance(strategies, list):
        for raw in strategies:
            if not isinstance(raw, dict):
                continue
            strategy_id = str(raw.get("id", ""))
            strategy_client_catalog[strategy_id] = {
                "parameter_schema": raw.get("parameter_schema", {}),
                "parameter_guide": raw.get("parameter_guide", []),
                "presets": raw.get("presets", []),
            }
            disabled = "" if raw.get("runnable_now") is True else " disabled"
            badge = "beginner" if raw.get("beginner_friendly") is True else "advanced"
            strategy_options += (
                f'<option value="{_e(strategy_id)}" '
                f'data-description="{_e(str(raw.get("explain_like_beginner", "")))}" '
                f'data-risk="{_e(str(raw.get("risk_notes", "")))}"{disabled}>'
                f'{_e(strategy_id)} · {_e(badge)}</option>'
            )
    datasets = _managed_dataset_catalog(context).list_datasets()
    ready_dataset_ids = [
        str(option.get("dataset_id", ""))
        for option in datasets
        if option.get("status") == "ready"
    ]
    preferred_dataset_id = (
        "bitstamp-btcusd-1d-10y"
        if any(
            option.get("dataset_id") == "bitstamp-btcusd-1d-10y"
            and option.get("standard_ready") is True
            for option in datasets
        )
        else (ready_dataset_ids[0] if ready_dataset_ids else "")
    )
    data_options = "".join(
        f'<option value="{_e(str(option.get("dataset_id", "")))}" '
        f'data-status="{_e(str(option.get("status", "")))}"'
        f'{" selected" if option.get("dataset_id") == preferred_dataset_id else ""}'
        f'{" disabled" if option.get("status") != "ready" else ""}>'
        f'{_e(str(option.get("symbol", "")))} · '
        f'{_e(str(option.get("timeframe", "")))} · '
        f'{_e(str(option.get("status", "")))} · {option.get("rows", 0)} rows'
        f'</option>'
        for option in datasets
    )
    if not data_options:
        data_options = '<option value="">No managed datasets found</option>'
    standard_ready = any(
        item.get("standard_ready") is True for item in datasets
    )
    standard_notice = (
        "10-year Bitstamp standard is verified and ready."
        if standard_ready
        else (
            "10-year standard is not installed. Standard validation stays "
            "disabled until Bitstamp BTC/USD daily data passes its manifest."
        )
    )
    strategy_catalog_json = json.dumps(
        strategy_client_catalog,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QT — BTC Research Flight Plan</title>
  <style>
    :root {{ color-scheme:dark; --navy:#08111f; --deck:#0e1b2e; --panel:#13243a; --line:#29405e; --ink:#f4f7fb; --muted:#a8b8cc; --blue:#4f7cff; --cyan:#38d6c8; --amber:#f2a93b; --bad:#ff6b6b; font-family:Inter, Aptos, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:radial-gradient(circle at 70% 0%, #162c49 0, var(--navy) 42%); color:var(--ink); min-height:100vh; }}
    main {{ width:min(1320px, calc(100vw - 32px)); margin:0 auto; padding:28px 0 56px; }}
    header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; margin-bottom:22px; }}
    h1 {{ margin:4px 0 8px; font:800 clamp(32px, 5vw, 64px)/.95 ui-rounded, "Arial Rounded MT Bold", sans-serif; letter-spacing:-.04em; }}
    h2 {{ font-size:18px; margin:0 0 8px; }}
    h3 {{ margin:0 0 6px; font-size:14px; }}
    a {{ color:var(--cyan); }}
    label {{ display:grid; gap:7px; color:var(--muted); font-weight:750; font-size:13px; }}
    select, input {{ width:100%; border:1px solid var(--line); border-radius:6px; padding:11px; background:#0b1728; color:var(--ink); font:inherit; }}
    button {{ border:0; border-radius:6px; padding:12px 16px; background:var(--blue); color:#fff; font:inherit; font-weight:850; cursor:pointer; }}
    button:disabled {{ opacity:.45; cursor:not-allowed; }}
    .subtle {{ color:var(--muted); font-size:13px; line-height:1.5; }}
    .shell {{ display:grid; grid-template-columns:210px minmax(0, 1fr) 280px; gap:14px; align-items:start; }}
    .panel {{ border:1px solid var(--line); background:color-mix(in srgb, var(--panel) 92%, transparent); border-radius:8px; padding:16px; box-shadow:0 18px 60px #02071155; }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
    .three {{ display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:12px; }}
    .mono {{ font-family:"SF Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .pill {{ display:inline-flex; border:1px solid #38d6c866; border-radius:999px; padding:4px 9px; color:var(--cyan); font-weight:850; font-size:11px; text-transform:uppercase; letter-spacing:.08em; }}
    .notice {{ border-left:3px solid var(--amber); padding:10px 12px; background:#f2a93b15; color:#ffd899; font-size:13px; margin-top:12px; }}
    .rail {{ position:sticky; top:16px; display:grid; gap:4px; }}
    .rail-item {{ display:grid; grid-template-columns:28px 1fr; gap:8px; padding:10px; border-left:2px solid var(--line); color:var(--muted); }}
    .rail-item strong {{ color:var(--ink); font-size:13px; }}
    .rail-num {{ width:24px; height:24px; display:grid; place-items:center; border:1px solid var(--line); border-radius:50%; font:700 11px "SF Mono", monospace; }}
    .stage {{ margin-bottom:12px; }}
    .stage-head {{ display:flex; align-items:center; gap:9px; margin-bottom:12px; }}
    .stage-head span {{ color:var(--cyan); font:700 11px "SF Mono", monospace; }}
    .mode-row {{ display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; }}
    .mode-card {{ display:block; border:1px solid var(--line); border-radius:7px; padding:11px; cursor:pointer; }}
    .mode-card:has(input:checked) {{ border-color:var(--cyan); background:#38d6c811; }}
    .mode-card input {{ width:auto; margin-right:6px; }}
    .evidence {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:4px; margin-bottom:14px; }}
    .gate {{ min-height:68px; padding:9px; border-top:3px solid var(--line); background:#091525; }}
    .gate.active {{ border-color:var(--cyan); }}
    .gate b {{ display:block; font-size:12px; margin-bottom:4px; }}
    .hidden {{ display:none !important; }}
    .rule-row {{ display:grid; grid-template-columns:1fr 120px; gap:8px; margin-top:8px; }}
    .parameter-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:12px; }}
    .parameter-card {{ border:1px solid var(--line); border-radius:7px; padding:10px; }}
    .parameter-card input {{ margin-top:7px; }}
    .status {{ margin-top:12px; font-weight:800; color:var(--cyan); }}
    .progress {{ height:7px; background:#07101d; border-radius:99px; overflow:hidden; margin-top:10px; }}
    .progress > div {{ height:100%; width:0; background:linear-gradient(90deg,var(--blue),var(--cyan)); transition:width .3s ease; }}
    .facts {{ display:grid; gap:10px; }}
    .fact {{ border-bottom:1px solid var(--line); padding-bottom:9px; }}
    .fact b {{ display:block; font-size:12px; margin-bottom:4px; }}
    @media (max-width: 980px) {{ .shell {{ grid-template-columns:1fr; }} .rail {{ position:static; grid-template-columns:repeat(5,1fr); }} .rail-item {{ display:block; }} .rail-item div:last-child {{ display:none; }} }}
    @media (max-width: 680px) {{ header, .grid, .three, .rule-row, .mode-row, .evidence {{ display:block; }} .panel, label, .gate {{ margin-top:9px; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <span class="pill">Private · spot long/cash · research only</span>
        <h1>BTC Research Flight Plan</h1>
        <div class="subtle">Build one reproducible historical experiment. A passing backtest is evidence—not a forecast and never a live-trading approval.</div>
      </div>
      <div class="subtle mono"><a href="/backtest">classic view</a> · <a href="/api/v2/backtests/catalog">v2 catalog</a> · <a href="/api/v2/backtests/health">health</a></div>
    </header>
    <div class="evidence" aria-label="Validation pipeline">
      <div class="gate active"><b>DATA</b><span class="subtle">schema + fingerprint</span></div>
      <div class="gate"><b>SIMULATION</b><span class="subtle">strategy + benchmarks</span></div>
      <div class="gate"><b>ROBUSTNESS</b><span class="subtle">bias + costs + paths</span></div>
      <div class="gate"><b>VERDICT</b><span class="subtle">exact pass/fail gates</span></div>
    </div>
    <div class="shell">
      <nav class="rail" aria-label="Five research steps">
        <div class="rail-item"><div class="rail-num">1</div><div><strong>1 · Choose your goal</strong><div class="subtle">Know the claim</div></div></div>
        <div class="rail-item"><div class="rail-num">2</div><div><strong>2 · Select verified data</strong><div class="subtle">Verify provenance</div></div></div>
        <div class="rail-item"><div class="rail-num">3</div><div><strong>3 · Build the strategy</strong><div class="subtle">Choose identity</div></div></div>
        <div class="rail-item"><div class="rail-num">4</div><div><strong>4 · Set assumptions</strong><div class="subtle">Control behavior</div></div></div>
        <div class="rail-item"><div class="rail-num">5</div><div><strong>5 · Review and run</strong><div class="subtle">Review assumptions</div></div></div>
      </nav>
      <div>
        <section class="panel stage">
          <div class="stage-head"><span>STEP 01</span><h2>Choose the research goal</h2></div>
          <label>What are you trying to learn?
            <select id="goal"><option>Compare a strategy with passive BTC ownership</option><option>Reduce drawdown while staying profitable</option><option>Learn how an indicator changes exposure</option></select>
          </label>
          <div class="notice">Backtests describe this historical sample. They cannot prove future profit, fill quality, or operational safety.</div>
        </section>
        <section class="panel stage">
          <div class="stage-head"><span>STEP 02</span><h2>Select verified market data</h2></div>
          <label>Dataset
            <select id="dataset-id">{data_options}</select>
          </label>
          <div class="notice">{_e(standard_notice)}</div>
          <button id="sync-data" type="button" style="margin-top:12px;background:#0b746b">Sync 10-year Bitstamp data</button>
          <div id="sync-status" class="subtle" aria-live="polite" style="margin-top:8px"></div>
        </section>
        <section class="panel stage">
          <div class="stage-head"><span>STEP 03</span><h2>Choose how the algorithm is created</h2></div>
          <div class="mode-row">
            <label class="mode-card"><span><input type="radio" name="mode" value="template" checked> Template</span><div class="subtle">One built-in strategy with its true identity.</div></label>
            <label class="mode-card"><span><input type="radio" name="mode" value="custom_rules"> Custom rules</span><div class="subtle">A no-code recipe named custom_rule_recipe.</div></label>
            <label class="mode-card"><span><input type="radio" name="mode" value="ensemble"> Ensemble</span><div class="subtle">Weighted exposure from two or three compatible strategies.</div></label>
          </div>
          <div id="template-fields" style="margin-top:12px"><label>Template algorithm<select id="strategy-id">{strategy_options}</select></label></div>
          <div id="custom-fields" class="hidden" style="margin-top:12px">
            <div class="subtle">Entry uses ALL conditions. Exit uses ANY condition. Window means how many candles each indicator reads.</div>
            <div class="rule-row"><select id="entry-1"><option value="close_above_sma">Close above SMA</option><option value="rsi_below">RSI below threshold</option><option value="donchian_breakout">Donchian breakout</option></select><input id="entry-window-1" value="200" aria-label="Entry indicator window"></div>
            <div class="rule-row"><select id="exit-1"><option value="close_below_sma">Close below SMA</option><option value="rsi_above">RSI above threshold</option><option value="bollinger_upper_touch">Bollinger upper touch</option></select><input id="exit-window-1" value="200" aria-label="Exit indicator window"></div>
          </div>
          <div id="ensemble-fields" class="hidden grid" style="margin-top:12px">
            <label>Component 1<select id="ensemble-1">{strategy_options}</select></label>
            <label>Weight<input id="ensemble-weight-1" value="60" inputmode="decimal"></label>
            <label>Component 2<select id="ensemble-2">{strategy_options}</select></label>
            <label>Weight<input id="ensemble-weight-2" value="40" inputmode="decimal"></label>
          </div>
        </section>
        <section class="panel stage">
          <div class="stage-head"><span>STEP 04</span><h2>Control assumptions and parameters</h2></div>
          <div class="three">
            <label>Initial cash <input id="initial-cash" value="10000" inputmode="decimal"><span class="subtle">Simulated USD/USDT starting balance.</span></label>
            <label>Fee (bps) <input id="fee-bps" value="10" inputmode="decimal"><span class="subtle">10 bps = 0.10% each fill.</span></label>
            <label>Slippage (bps) <input id="slippage-bps" value="5" inputmode="decimal"><span class="subtle">Extra adverse fill movement.</span></label>
          </div>
          <div class="subtle" id="strategy-explanation" style="margin-top:12px"></div>
          <div id="template-parameters" class="parameter-grid" aria-live="polite"></div>
        </section>
        <section class="panel stage">
          <div class="stage-head"><span>STEP 05</span><h2>Review and launch</h2></div>
          <div class="grid">
            <label>Validation profile
              <select id="validation-profile"><option value="quick">Quick · replay + benchmarks</option><option value="standard"{" selected" if standard_ready else " disabled"}>Standard · full evidence pipeline</option></select>
            </label>
            <label>Deterministic seed<input id="seed" value="7" inputmode="numeric"></label>
          </div>
          <button id="run-job" type="button" style="margin-top:14px">Launch historical research</button>
          <button id="cancel-job" type="button" class="hidden" style="margin-top:14px;background:#5c3340">Cancel job</button>
          <div id="builder-status" class="status" aria-live="polite">Ready for a quick diagnostic run.</div>
          <div class="progress"><div id="job-progress"></div></div>
        </section>
      </div>
      <aside class="panel">
        <span class="pill">Evidence brief</span>
        <div class="facts" style="margin-top:14px">
          <div class="fact"><b>Market</b><span class="subtle">BTC spot, long or cash only</span></div>
          <div class="fact"><b>Benchmarks</b><span class="subtle">Buy-and-hold and fixed DCA</span></div>
          <div class="fact"><b>Default costs</b><span class="subtle">0.10% fee + 0.05% slippage</span></div>
          <div class="fact"><b>Standard gates</b><span class="subtle">Walk-forward, sensitivity, 500 paths, cost stress, future-data and warm-up consistency</span></div>
          <div class="fact"><b>Safety boundary</b><span class="subtle">No keys, leverage, shorts, exchange connector, or real orders</span></div>
        </div>
      </aside>
    </div>
    <script>
      (function () {{
        var strategyCatalog = {strategy_catalog_json};
        var currentJob = null;
        function mode() {{ return document.querySelector('input[name="mode"]:checked').value; }}
        function syncMode() {{
          ['template', 'custom', 'ensemble'].forEach(function (name) {{
            document.getElementById(name + '-fields').classList.toggle('hidden', mode() !== (name === 'custom' ? 'custom_rules' : name));
          }});
        }}
        document.querySelectorAll('input[name="mode"]').forEach(function (node) {{ node.addEventListener('change', syncMode); }});
        function condition(selectId, windowId) {{
          var indicator = document.getElementById(selectId).value;
          if (!indicator) return null;
          return {{ indicator: indicator, window: Number(document.getElementById(windowId).value || 0) }};
        }}
        function payload() {{
          var value = {{
            dataset_id: document.getElementById('dataset-id').value,
            mode: mode(),
            validation_profile: document.getElementById('validation-profile').value,
            seed: Number(document.getElementById('seed').value || 7),
            assumptions: {{
              initial_cash: Number(document.getElementById('initial-cash').value || 10000),
              fee_bps: Number(document.getElementById('fee-bps').value || 10),
              slippage_bps: Number(document.getElementById('slippage-bps').value || 5)
            }}
          }};
          if (value.mode === 'template') value.template = {{
            strategy_id: document.getElementById('strategy-id').value,
            parameters: readTemplateParameters()
          }};
          if (value.mode === 'custom_rules') value.rules = {{
              entry: {{ operator: 'ALL', conditions: [condition('entry-1', 'entry-window-1')].filter(Boolean) }},
              exit: {{ operator: 'ANY', conditions: [condition('exit-1', 'exit-window-1')].filter(Boolean) }}
          }};
          if (value.mode === 'ensemble') value.ensemble = {{ components: [
            {{ strategy_id: document.getElementById('ensemble-1').value, parameters: {{}}, weight: Number(document.getElementById('ensemble-weight-1').value || 0) }},
            {{ strategy_id: document.getElementById('ensemble-2').value, parameters: {{}}, weight: Number(document.getElementById('ensemble-weight-2').value || 0) }}
          ] }};
          return value;
        }}
        function readTemplateParameters() {{
          var parameters = {{}};
          document.querySelectorAll('#template-parameters [data-parameter]').forEach(function (input) {{
            var raw = input.value;
            if (input.dataset.kind === 'array') {{
              parameters[input.dataset.parameter] = raw.split(',').map(function (item) {{ return Number(item.trim()); }}).filter(Number.isFinite);
              return;
            }}
            parameters[input.dataset.parameter] = Number(raw);
          }});
          return parameters;
        }}
        function numericRule(schema) {{
          if (schema.type === 'integer' || schema.type === 'number') return schema;
          return (schema.anyOf || []).find(function (item) {{ return item.type === 'number' || item.type === 'integer'; }}) || {{}};
        }}
        function renderTemplateParameters() {{
          var strategyId = document.getElementById('strategy-id').value;
          var metadata = strategyCatalog[strategyId] || {{}};
          var properties = (metadata.parameter_schema || {{}}).properties || {{}};
          var guide = metadata.parameter_guide || [];
          var impactByName = {{}};
          guide.forEach(function (item) {{ impactByName[item.name] = item.impact; }});
          var root = document.getElementById('template-parameters');
          root.replaceChildren();
          Object.keys(properties).forEach(function (name) {{
            var schema = properties[name] || {{}};
            var numeric = numericRule(schema);
            var card = document.createElement('label');
            card.className = 'parameter-card';
            var title = document.createElement('span');
            title.textContent = (schema.title || name) + ' · default ' + String(schema.default);
            var input = document.createElement('input');
            input.dataset.parameter = name;
            input.dataset.kind = schema.type === 'array' ? 'array' : 'number';
            input.type = schema.type === 'array' ? 'text' : 'number';
            input.value = Array.isArray(schema.default) ? schema.default.join(', ') : String(schema.default);
            if (numeric.minimum !== undefined) input.min = String(numeric.minimum);
            if (numeric.exclusiveMinimum !== undefined) input.min = String(Number(numeric.exclusiveMinimum) + 0.000001);
            if (numeric.maximum !== undefined) input.max = String(numeric.maximum);
            input.step = schema.type === 'integer' ? '1' : 'any';
            var help = document.createElement('span');
            help.className = 'subtle';
            help.textContent = impactByName[name] || 'Change one input at a time and compare the evidence.';
            card.append(title, input, help);
            root.appendChild(card);
          }});
          if (!Object.keys(properties).length) {{
            root.innerHTML = '<div class="subtle">This strategy has no configurable parameters.</div>';
          }}
        }}
        function poll(jobId) {{
          fetch('/api/v2/backtests/jobs/' + jobId).then(function (res) {{ return res.json(); }}).then(function (data) {{
            var job = data.job || {{}};
            document.getElementById('builder-status').textContent = (job.stage || job.status) + ' · ' + (job.progress || 0) + '%';
            document.getElementById('job-progress').style.width = (job.progress || 0) + '%';
            if (job.status === 'complete' && job.result && job.result.run_id) {{
              window.location.href = '/backtest/runs/' + job.result.run_id;
              return;
            }}
            if (job.status === 'failed' || job.status === 'cancelled') {{
              document.getElementById('builder-status').textContent = 'Failed: ' + (job.error || 'unknown error');
              return;
            }}
            window.setTimeout(function () {{ poll(jobId); }}, 700);
          }}).catch(function () {{ document.getElementById('builder-status').textContent = 'Connection lost. Retrying…'; window.setTimeout(function () {{ poll(jobId); }}, 1200); }});
        }}
        document.getElementById('strategy-id').addEventListener('change', function (event) {{
          var selected = event.target.options[event.target.selectedIndex];
          document.getElementById('strategy-explanation').textContent = selected.dataset.description || '';
          renderTemplateParameters();
        }});
        document.getElementById('strategy-id').dispatchEvent(new Event('change'));
        document.getElementById('sync-data').addEventListener('click', function () {{
          var button = this;
          var status = document.getElementById('sync-status');
          button.disabled = true;
          status.textContent = 'Queueing the verified Bitstamp download…';
          fetch('/api/v2/backtests/datasets/bitstamp-btcusd-1d-10y/sync', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: '{{}}'
          }}).then(function (res) {{ return res.json(); }}).then(function (data) {{
            if (!data.ok || !data.job) {{
              status.textContent = 'Sync could not start: ' + ((data.errors || []).join('; ') || 'unknown error');
              button.disabled = false;
              return;
            }}
            function pollSync() {{
              fetch('/api/v2/backtests/jobs/' + data.job.job_id).then(function (res) {{ return res.json(); }}).then(function (current) {{
                var job = current.job || {{}};
                status.textContent = (job.stage || job.status) + ' · ' + (job.progress || 0) + '%';
                if (job.status === 'complete') {{
                  status.textContent = 'Dataset verified. Reloading the catalog…';
                  window.location.reload();
                  return;
                }}
                if (job.status === 'failed' || job.status === 'cancelled') {{
                  status.textContent = 'Sync failed: ' + (job.error || 'unknown error');
                  button.disabled = false;
                  return;
                }}
                window.setTimeout(pollSync, 900);
              }}).catch(function () {{
                status.textContent = 'Connection lost while checking sync. Retrying…';
                window.setTimeout(pollSync, 1500);
              }});
            }}
            pollSync();
          }}).catch(function () {{
            status.textContent = 'Sync request failed. Check the worker health and retry.';
            button.disabled = false;
          }});
        }});
        document.getElementById('run-job').addEventListener('click', function () {{
          var status = document.getElementById('builder-status');
          status.textContent = 'Submitting research job...';
          fetch('/api/v2/backtests/jobs', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload())
          }}).then(function (res) {{ return res.json(); }}).then(function (data) {{
            if (!data.ok) {{
              status.textContent = 'Cannot run: ' + ((data.errors || []).join('; ') || 'invalid request');
              return;
            }}
            currentJob = data.job.job_id;
            document.getElementById('cancel-job').classList.remove('hidden');
            poll(currentJob);
          }});
        }});
        document.getElementById('cancel-job').addEventListener('click', function () {{
          if (!currentJob) return;
          fetch('/api/v2/backtests/jobs/' + currentJob + '/cancel', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}' }});
        }});
      }})();
    </script>
  </main>
</body>
</html>
"""


def _render_research_run_page(
    context: DashboardContext,
    summary: Mapping[str, object],
) -> str:
    chart_payload = _backtest_chart_payload(context, summary)
    metrics = summary.get("metrics")
    metrics_map = metrics if isinstance(metrics, dict) else {}
    counts = summary.get("counts")
    counts_map = counts if isinstance(counts, dict) else {}
    verdict = summary.get("verdict")
    verdict_map = verdict if isinstance(verdict, dict) else {}
    validation = summary.get("validation")
    validation_map = validation if isinstance(validation, dict) else {}
    benchmarks = summary.get("benchmarks")
    benchmarks_map = benchmarks if isinstance(benchmarks, dict) else {}
    data = summary.get("data")
    data_map = data if isinstance(data, dict) else {}
    scorecard = summary.get("scorecard")
    scorecard_map = scorecard if isinstance(scorecard, dict) else _research_scorecard(summary)
    strategy = str(summary.get("strategy") or summary.get("engine_strategy") or "")
    run_id = str(summary.get("run_id", ""))
    trade_count = metrics_map.get("trade_count", counts_map.get("trades", 0))
    verdict_label = str(
        verdict_map.get("label")
        or scorecard_map.get("verdict")
        or "No evidence verdict is available."
    )
    failed_gates = verdict_map.get("failed_gates")
    failed_gate_items = (
        "".join(f"<li>{_e(str(item))}</li>" for item in failed_gates)
        if isinstance(failed_gates, list) and failed_gates
        else "<li>All configured verdict gates passed.</li>"
    )
    buy_hold = benchmarks_map.get("buy_and_hold")
    buy_hold_map = buy_hold if isinstance(buy_hold, dict) else {}
    fixed_dca = benchmarks_map.get("fixed_dca")
    fixed_dca_map = fixed_dca if isinstance(fixed_dca, dict) else {}
    buy_hold_metrics = buy_hold_map.get("metrics")
    buy_hold_metric_map = buy_hold_metrics if isinstance(buy_hold_metrics, dict) else {}
    dca_metrics = fixed_dca_map.get("metrics")
    dca_metric_map = dca_metrics if isinstance(dca_metrics, dict) else {}
    monthly_rows, trade_rows = _research_run_tables(context, run_id)
    robustness_visuals = _robustness_visuals(validation_map)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QT - Backtest Result</title>
  <style>
    :root {{ color-scheme:light; --bg:#f5f4ef; --ink:#18211d; --muted:#657268; --line:#ddd8cc; --panel:#fffdf8; --accent:#0b746b; --bad:#a43e36; --good:#166c4f; font-family:Aptos, Inter, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); }}
    main {{ width:min(1180px, calc(100vw - 32px)); margin:0 auto; padding:28px 0 52px; }}
    header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-end; margin-bottom:18px; }}
    h1 {{ margin:0; font-size:clamp(30px, 4vw, 52px); line-height:1; letter-spacing:0; }}
    h2 {{ margin:28px 0 12px; font-size:20px; }}
    a {{ color:var(--accent); }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th, td {{ border-bottom:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; font-size:13px; }}
    th {{ color:var(--muted); background:#fbf7ed; }}
    .subtle {{ color:var(--muted); font-size:14px; }}
    .panel {{ border:1px solid var(--line); background:var(--panel); border-radius:8px; padding:16px; }}
    .grid {{ display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; }}
    .metric {{ font-size:24px; font-weight:900; margin-top:7px; }}
    .mono {{ font-family:"SF Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .pill {{ display:inline-flex; border-radius:999px; padding:3px 9px; font-weight:850; font-size:12px; }}
    .good {{ color:var(--good); background:#e5f3ed; }}
    .warn {{ color:#805000; background:#fff1cf; }}
    .chart-shell {{ min-height:420px; border:1px solid var(--line); border-radius:8px; background:#fff; padding:10px; }}
    #price-chart {{ min-height:390px; }}
    #monte-carlo-chart {{ min-height:260px; }}
    .heatmap {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; margin-top:10px; }}
    .heat-cell {{ border:1px solid var(--line); border-radius:7px; padding:10px; min-height:78px; }}
    @media (max-width: 860px) {{ header, .grid {{ display:block; }} .panel {{ margin-top:10px; }} table {{ display:block; overflow-x:auto; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <span class="pill good">{_e(str(verdict_map.get("status", "research verdict")))}</span>
        <h1>{_e(strategy)} result</h1>
        <div class="subtle mono">{_e(run_id)} · {_e(str(data_map.get("dataset_id", summary.get("ohlcv_key", ""))))}</div>
      </div>
      <div class="subtle mono"><a href="/backtest/build">build another</a> · <a href="/api/v2/backtests/runs/{_e(run_id)}">result JSON</a></div>
    </header>
    <section class="grid">
      {_card("Return", _fmt_pct(metrics_map.get("total_return")))}
      {_card("Max drawdown", _fmt_pct(metrics_map.get("max_drawdown")))}
      {_card("Sharpe", _fmt_num(metrics_map.get("sharpe")))}
      {_card("Trades", _e(str(trade_count)))}
    </section>
    <h2>Research verdict</h2>
    <section class="panel">
      <div class="metric">{_e(str(verdict_map.get("status", scorecard_map.get("score", "unscored"))))}</div>
      <div class="subtle">{_e(verdict_label)}</div>
      <ul>{failed_gate_items}</ul>
      <table style="margin-top:12px"><tbody>
        <tr><th>Data integrity</th><td>{_e("pass" if validation_map.get("data_complete") is True else "fail")}</td></tr>
        <tr><th>Future-data consistency</th><td>{_e("pass" if validation_map.get("lookahead_consistent") is True else "fail or not run")}</td></tr>
        <tr><th>Warm-up stability</th><td>{_e("pass" if validation_map.get("recursive_stable") is True else "fail or not run")}</td></tr>
        <tr><th>Validation profile complete</th><td>{_e("yes" if validation_map.get("profile_complete") is True else "quick diagnostic only")}</td></tr>
      </tbody></table>
    </section>
    <h2>Price, volume, equity, and drawdown</h2>
    <section class="chart-shell"><div id="price-chart"></div></section>
    <h2>Strategy versus passive benchmarks</h2>
    <section class="panel">
      <table><thead><tr><th>Approach</th><th>Total return</th><th>Max drawdown</th><th>Sharpe</th><th>Fees</th></tr></thead><tbody>
        <tr><td>{_e(strategy)}</td><td>{_fmt_pct(metrics_map.get("total_return"))}</td><td>{_fmt_pct(metrics_map.get("max_drawdown"))}</td><td>{_fmt_num(metrics_map.get("sharpe"))}</td><td>{_fmt_num(metrics_map.get("total_fees"))}</td></tr>
        <tr><td>Buy and hold</td><td>{_fmt_pct(buy_hold_metric_map.get("total_return"))}</td><td>{_fmt_pct(buy_hold_metric_map.get("max_drawdown"))}</td><td>{_fmt_num(buy_hold_metric_map.get("sharpe"))}</td><td>{_fmt_num(buy_hold_metric_map.get("total_fees"))}</td></tr>
        <tr><td>Fixed DCA</td><td>{_fmt_pct(dca_metric_map.get("total_return"))}</td><td>{_fmt_pct(dca_metric_map.get("max_drawdown"))}</td><td>{_fmt_num(dca_metric_map.get("sharpe"))}</td><td>{_fmt_num(dca_metric_map.get("total_fees"))}</td></tr>
      </tbody></table>
    </section>
    <h2>Robustness checklist</h2>
    <section class="panel">
      {robustness_visuals}
      <div id="monte-carlo-chart" aria-label="Monte Carlo percentile paths"></div>
      <div class="subtle">The Monte Carlo chart shows pessimistic 5th percentile, median, and optimistic 95th percentile bootstrapped paths. It is a distribution of historical return blocks, not a price forecast.</div>
    </section>
    <h2>Monthly returns and trade ledger</h2>
    <section class="panel">
      <h3>Recent monthly strategy returns</h3>
      <table><thead><tr><th>UTC month</th><th>Return</th></tr></thead><tbody>{monthly_rows}</tbody></table>
      <h3 style="margin-top:18px">Trades and fills</h3>
      <table><thead><tr><th>UTC time</th><th>Side</th><th>Quantity</th><th>Price</th><th>Fee</th><th>P&amp;L</th><th>Reason</th><th>Holding hours</th></tr></thead><tbody>{trade_rows}</tbody></table>
    </section>
    <h2>Data provenance and reproducibility</h2>
    <section class="panel">
      <table><tbody>
        <tr><th>Source</th><td>{_e(str(data_map.get("provider", "")))} · {_e(str(data_map.get("symbol", "")))} · {_e(str(data_map.get("timeframe", "")))}</td></tr>
        <tr><th>Coverage</th><td>{_e(str(data_map.get("start", "")))} → {_e(str(data_map.get("end", "")))} · {_e(str(data_map.get("rows", "")))} rows</td></tr>
        <tr><th>Fingerprint</th><td class="mono">{_e(str(data_map.get("fingerprint", "")))}</td></tr>
        <tr><th>Artifacts</th><td><a href="/api/v2/backtests/runs/{_e(run_id)}/artifacts/summary.json">summary</a> · <a href="/api/v2/backtests/runs/{_e(run_id)}/artifacts/data_manifest.json">manifest</a> · <a href="/api/v2/backtests/runs/{_e(run_id)}/artifacts/equity.csv">equity</a> · <a href="/api/v2/backtests/runs/{_e(run_id)}/artifacts/trades.csv">trades</a></td></tr>
      </tbody></table>
      <div class="subtle" style="margin-top:10px">This result is research evidence only. It is never labeled live-ready.</div>
    </section>
    <script type="application/json" id="backtest-chart-data">{_json_script(chart_payload)}</script>
    <script type="application/json" id="validation-chart-data">{_json_script(validation_map)}</script>
    <script src="/static/lightweight-charts-5.2.0.js"></script>
    <script>
      (function () {{
        var node = document.getElementById('price-chart');
        var dataNode = document.getElementById('backtest-chart-data');
        if (!node || !dataNode || !window.LightweightCharts) {{ if (node) node.innerHTML = '<div class="subtle" style="padding:18px">Chart library unavailable.</div>'; return; }}
        var payload = JSON.parse(dataNode.textContent || '{{}}');
        if (!payload.candles || !payload.candles.length) {{ node.innerHTML = '<div class="subtle" style="padding:18px">No chart data.</div>'; return; }}
        var chart = LightweightCharts.createChart(node, {{ height:620, layout:{{ background:{{ type:'solid', color:'#ffffff' }}, textColor:'#28332d' }}, grid:{{ vertLines:{{ color:'#eee7db' }}, horzLines:{{ color:'#eee7db' }} }} }});
        var candles = chart.addSeries(LightweightCharts.CandlestickSeries, {{ upColor:'#1f9d6a', downColor:'#d05a3b', borderVisible:false, wickUpColor:'#1f9d6a', wickDownColor:'#d05a3b' }});
        candles.setData(payload.candles);
        if (payload.volume && payload.volume.length) {{
          var volume = chart.addSeries(LightweightCharts.HistogramSeries, {{ priceFormat:{{type:'volume'}}, priceScaleId:'' }}, 1);
          volume.setData(payload.volume);
        }}
        if (payload.equity && payload.equity.length) {{
          var equity = chart.addSeries(LightweightCharts.LineSeries, {{ color:'#2f6bff', lineWidth:2, title:'Strategy' }}, 2);
          equity.setData(payload.equity);
        }}
        if (payload.benchmark_equity && payload.benchmark_equity.buy_and_hold) {{
          var hold = chart.addSeries(LightweightCharts.LineSeries, {{ color:'#f2a93b', lineWidth:1, title:'Buy & hold' }}, 2);
          hold.setData(payload.benchmark_equity.buy_and_hold);
          var dca = chart.addSeries(LightweightCharts.LineSeries, {{ color:'#38d6c8', lineWidth:1, title:'Fixed DCA' }}, 2);
          dca.setData(payload.benchmark_equity.fixed_dca || []);
        }}
        if (payload.drawdown && payload.drawdown.length) {{
          var drawdown = chart.addSeries(LightweightCharts.AreaSeries, {{ lineColor:'#a43e36', topColor:'#a43e3633', bottomColor:'#a43e3605', title:'Drawdown' }}, 3);
          drawdown.setData(payload.drawdown);
        }}
        if (payload.markers && payload.markers.length) {{
          if (LightweightCharts.createSeriesMarkers) LightweightCharts.createSeriesMarkers(candles, payload.markers);
          else if (candles.setMarkers) candles.setMarkers(payload.markers);
        }}
        chart.timeScale().fitContent();
        var validationNode = document.getElementById('validation-chart-data');
        var monteNode = document.getElementById('monte-carlo-chart');
        var validation = validationNode ? JSON.parse(validationNode.textContent || '{{}}') : {{}};
        var paths = validation.monte_carlo && validation.monte_carlo.percentile_paths;
        if (monteNode && paths && paths.p05 && paths.p05.length) {{
          var monte = LightweightCharts.createChart(monteNode, {{ height:260, layout:{{ background:{{ type:'solid', color:'#fffdf8' }}, textColor:'#28332d' }}, grid:{{ vertLines:{{ color:'#eee7db' }}, horzLines:{{ color:'#eee7db' }} }} }});
          [['p05','#a43e36'],['p50','#0b746b'],['p95','#2f6bff']].forEach(function (entry) {{
            var line = monte.addSeries(LightweightCharts.LineSeries, {{ color:entry[1], lineWidth:entry[0] === 'p50' ? 3 : 1, title:entry[0].toUpperCase() }});
            line.setData(paths[entry[0]].map(function (value, index) {{ return {{ time:946684800 + index * 86400, value:value }}; }}));
          }});
          monte.timeScale().fitContent();
        }} else if (monteNode) {{
          monteNode.innerHTML = '<div class="subtle" style="padding:18px">Run the standard profile to generate 500 Monte Carlo paths.</div>';
        }}
      }})();
    </script>
  </main>
</body>
</html>
"""


def _research_run_tables(
    context: DashboardContext,
    run_id: str,
) -> tuple[str, str]:
    if not _is_opaque_run_id(run_id):
        return (
            '<tr><td colspan="2">No monthly return data.</td></tr>',
            '<tr><td colspan="8">No trade data.</td></tr>',
        )
    run_dir = context.backtests_dir / run_id
    monthly_rows = _monthly_return_rows(run_dir / "equity.csv")
    trade_rows = _trade_ledger_rows(run_dir / "trades.csv")
    return monthly_rows, trade_rows


def _monthly_return_rows(path: Path) -> str:
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError):
        return '<tr><td colspan="2">No monthly return data.</td></tr>'
    if frame.empty or not {"timestamp", "strategy"}.issubset(frame.columns):
        return '<tr><td colspan="2">No monthly return data.</td></tr>'
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"],
        utc=True,
        errors="coerce",
    )
    frame = frame.dropna(subset=["timestamp"])
    values = pd.to_numeric(frame["strategy"], errors="coerce")
    series = pd.Series(
        values.to_numpy(),
        index=pd.DatetimeIndex(frame["timestamp"]),
    ).dropna()
    if series.empty:
        return '<tr><td colspan="2">No monthly return data.</td></tr>'
    returns = series.resample("ME").last().pct_change().dropna().tail(24)
    if returns.empty:
        return '<tr><td colspan="2">Not enough history for monthly returns.</td></tr>'
    return "".join(
        f"<tr><td>{_e(timestamp.strftime('%Y-%m'))}</td>"
        f"<td>{_fmt_pct(value)}</td></tr>"
        for timestamp, value in returns.items()
    )


def _trade_ledger_rows(path: Path) -> str:
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError):
        return '<tr><td colspan="8">No trade data.</td></tr>'
    if frame.empty:
        return '<tr><td colspan="8">This strategy placed no trades.</td></tr>'
    rows: list[str] = []
    for _, row in frame.tail(100).iterrows():
        rows.append(
            "<tr>"
            f"<td>{_e(str(row.get('ts', '')))}</td>"
            f"<td>{_e(str(row.get('side', '')))}</td>"
            f"<td>{_fmt_num(row.get('qty'))}</td>"
            f"<td>{_fmt_num(row.get('price'))}</td>"
            f"<td>{_fmt_num(row.get('fee'))}</td>"
            f"<td>{_fmt_num(row.get('pnl'))}</td>"
            f"<td>{_e(str(row.get('reason', '')))}</td>"
            f"<td>{_fmt_num(row.get('holding_hours'))}</td>"
            "</tr>"
        )
    return "".join(rows)


def _robustness_visuals(validation: Mapping[str, object]) -> str:
    walk_forward = validation.get("walk_forward")
    walk_map = walk_forward if isinstance(walk_forward, dict) else {}
    folds = walk_map.get("folds")
    fold_rows = ""
    if isinstance(folds, list):
        for index, raw in enumerate(folds, start=1):
            if not isinstance(raw, dict):
                continue
            metrics = raw.get("test_metrics")
            metric_map = metrics if isinstance(metrics, dict) else {}
            fold_rows += (
                f"<tr><td>{index}</td>"
                f"<td>{_e(str(raw.get('test_start', ''))[:10])}</td>"
                f"<td>{_fmt_pct(metric_map.get('total_return'))}</td>"
                f"<td>{_fmt_num(metric_map.get('sharpe'))}</td>"
                f"<td>{_fmt_pct(metric_map.get('max_drawdown'))}</td></tr>"
            )
    if not fold_rows:
        fold_rows = '<tr><td colspan="5">Run the standard profile to create expanding out-of-sample folds.</td></tr>'

    sensitivity = validation.get("sensitivity")
    sensitivity_map = sensitivity if isinstance(sensitivity, dict) else {}
    evaluations = sensitivity_map.get("evaluations")
    heat_cells = ""
    if isinstance(evaluations, list):
        for raw in evaluations:
            if not isinstance(raw, dict):
                continue
            metrics = raw.get("metrics")
            metric_map = metrics if isinstance(metrics, dict) else {}
            sharpe = _finite_float(metric_map.get("sharpe")) or 0.0
            hue = "#d9efe7" if sharpe >= 0 else "#f4d9d5"
            heat_cells += (
                f'<div class="heat-cell" style="background:{hue}">'
                f'<div class="mono">{_e(json.dumps(raw.get("parameters", {}), sort_keys=True, default=str))}</div>'
                f'<strong>Sharpe {_fmt_num(sharpe)}</strong> · '
                f'{_fmt_pct(metric_map.get("total_return"))}</div>'
            )
    if not heat_cells:
        heat_cells = '<div class="subtle">Run the standard profile to compare curated parameter variants.</div>'

    monte_carlo = validation.get("monte_carlo")
    monte_map = monte_carlo if isinstance(monte_carlo, dict) else {}
    cost_stress = validation.get("cost_stress")
    cost_map = cost_stress if isinstance(cost_stress, dict) else {}
    cost_rows = "".join(
        f"<tr><td>{_e(str(multiplier))}</td>"
        f"<td>{_fmt_pct(metrics.get('total_return') if isinstance(metrics, dict) else None)}</td>"
        f"<td>{_fmt_pct(metrics.get('max_drawdown') if isinstance(metrics, dict) else None)}</td></tr>"
        for multiplier, metrics in cost_map.items()
    )
    if not cost_rows:
        cost_rows = '<tr><td colspan="3">Run the standard profile for 1x, 2x, and 3x costs.</td></tr>'
    return f"""
      <h3>Purged walk-forward folds</h3>
      <table><thead><tr><th>Fold</th><th>Test starts</th><th>Return</th><th>Sharpe</th><th>Drawdown</th></tr></thead><tbody>{fold_rows}</tbody></table>
      <h3 style="margin-top:18px">Parameter sensitivity heatmap</h3>
      <div class="heatmap">{heat_cells}</div>
      <h3 style="margin-top:18px">500-path block bootstrap</h3>
      <div class="grid">
        {_card("Loss probability", _fmt_pct(monte_map.get("loss_probability")))}
        {_card("P05 return", _fmt_pct(monte_map.get("return_p05")))}
        {_card("Median return", _fmt_pct(monte_map.get("return_p50")))}
        {_card("P95 return", _fmt_pct(monte_map.get("return_p95")))}
      </div>
      <h3 style="margin-top:18px">Fee and slippage stress</h3>
      <table><thead><tr><th>Cost assumption</th><th>Return</th><th>Drawdown</th></tr></thead><tbody>{cost_rows}</tbody></table>
      <div style="margin-top:14px"><strong>Deflated Sharpe probability:</strong> {_fmt_pct(validation.get("deflated_sharpe_probability"))}</div>
    """


def _summary_from_result(result: JsonDict | None) -> Mapping[str, object] | None:
    if result is None or result.get("ok") is not True:
        return None
    summary = result.get("summary")
    return summary if isinstance(summary, dict) else None


def _request_from_result(result: JsonDict | None) -> Mapping[str, object]:
    if result is None:
        return {}
    request = result.get("request")
    return request if isinstance(request, dict) else {}


def _strategy_label(strategy: str) -> str:
    guide = _STRATEGY_GUIDE.get(strategy)
    if guide is None:
        return strategy
    return f"{guide['label']} ({strategy})"


def _strategy_explainer_card(strategy: str) -> str:
    guide = _STRATEGY_GUIDE.get(strategy, _STRATEGY_GUIDE["composite"])
    return (
        '<div class="panel">'
        '<div class="eyebrow">Algorithm</div>'
        f'<h3>{_e(guide["label"])}</h3>'
        f'<div class="subtle">{_e(guide["plain"])}</div>'
        '<table style="margin-top:10px"><tbody>'
        f'<tr><th>Good for</th><td>{_e(guide["best_for"])}</td></tr>'
        f'<tr><th>Main risk</th><td>{_e(guide["risk"])}</td></tr>'
        '</tbody></table>'
        '</div>'
    )


def _data_source_card(option: Mapping[str, object] | None) -> str:
    if option is None:
        return (
            '<div class="panel"><div class="eyebrow">Data source</div>'
            '<h3>No runnable OHLCV data</h3>'
            '<div class="subtle">Fetch or restore a non-empty local OHLCV parquet file before running.</div></div>'
        )
    key = str(option.get("key", ""))
    rows = _as_int(option.get("rows"))
    start = str(option.get("start", ""))
    end = str(option.get("end", ""))
    provider = "OKX public BTC/USDT candles" if key.startswith("okx_") else "Local parquet OHLCV"
    return (
        '<div class="panel">'
        '<div class="eyebrow">Data source</div>'
        f'<h3 class="mono">{_e(key)}</h3>'
        f'<div class="subtle">{_e(provider)}. Period: {_e(_timeframe_label(key))}. '
        'This is historical market data, not a forecast feed.</div>'
        '<table style="margin-top:10px"><tbody>'
        f'<tr><th>Rows</th><td>{rows:,}</td></tr>'
        f'<tr><th>Window</th><td class="mono">{_e(start)} → {_e(end)}</td></tr>'
        '</tbody></table>'
        '</div>'
    )


def _option_for_key(
    options: Sequence[Mapping[str, object]],
    key: str,
) -> Mapping[str, object] | None:
    for option in options:
        if option.get("key") == key:
            return option
    return None


def _timeframe_label(key: str) -> str:
    if key.endswith("_1h"):
        return "1-hour candles"
    if key.endswith("_4h"):
        return "4-hour candles"
    if key.endswith("_1d"):
        return "1-day candles"
    return "local candle interval"


def _render_backtest_config_guide() -> str:
    return """
    <div class="grid">
      <div class="panel"><div class="eyebrow">Strategy</div><h3>Which rule is being tested?</h3><div class="subtle">The strategy determines when the engine enters, exits, or stays in cash. Change only one strategy at a time when comparing results.</div></div>
      <div class="panel"><div class="eyebrow">Data source</div><h3>Which market history is replayed?</h3><div class="subtle">Only non-empty local OHLCV files are runnable. The current production-safe source is OKX BTC/USDT 1-hour candles.</div></div>
      <div class="panel"><div class="eyebrow">Initial cash</div><h3>What account size is simulated?</h3><div class="subtle">This scales position sizes and P&amp;L. It does not connect to a real wallet.</div></div>
      <div class="panel"><div class="eyebrow">Result</div><h3>What should you read first?</h3><div class="subtle">Start with drawdown, trade count, and data window before total return. A high return with high drawdown is not production-ready.</div></div>
    </div>
    """


def _render_strategy_guide_table() -> str:
    rows = []
    for strategy in _BACKTEST_STRATEGIES:
        guide = _STRATEGY_GUIDE[strategy]
        rows.append(
            "<tr>"
            f'<td class="mono">{_e(strategy)}</td>'
            f'<td>{_e(guide["label"])}</td>'
            f'<td>{_e(guide["plain"])}</td>'
            f'<td>{_e(guide["risk"])}</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>ID</th><th>Name</th><th>Algorithm</th>"
        f"<th>Main risk</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _result_explainer(summary: Mapping[str, object] | None) -> str:
    if summary is None:
        return (
            '<h3>Backtest flight recorder</h3>'
            '<div class="subtle">No completed run yet. Configure the replay above and run once; '
            'this panel will explain the result in plain language.</div>'
        )
    metrics = summary.get("metrics")
    counts = summary.get("counts")
    metrics_map = metrics if isinstance(metrics, dict) else {}
    counts_map = counts if isinstance(counts, dict) else {}
    total_return = _as_float(metrics_map.get("total_return"))
    max_drawdown = _as_float(metrics_map.get("max_drawdown"))
    sharpe = _as_float(metrics_map.get("sharpe"))
    trades = _as_int(counts_map.get("trades", summary.get("num_trades")))
    risk_class = "good" if max_drawdown > -0.15 else "warn" if max_drawdown > -0.35 else "bad"
    prediction = _prediction_sentence(total_return, sharpe, trades)
    risk = _risk_sentence(max_drawdown, sharpe, trades)
    return f"""
    <div class="grid">
      {_card("Prediction result", _e(prediction))}
      {_card("Risk readout", f'<span class="pill {risk_class}">{_e(risk)}</span>')}
      {_card("Data window", _e(str(summary.get("ohlcv_key", ""))))}
      {_card("Run id", f'<span class="mono">{_e(str(summary.get("run_id", "")))}</span>')}
    </div>
    <table style="margin-top:12px">
      <tbody>
        <tr><th>What was tested</th><td>{_e(str(summary.get("strategy") or summary.get("engine_strategy") or "composite"))} on {_e(str(summary.get("ohlcv_key", "")))}</td></tr>
        <tr><th>How to read it</th><td>Total return answers “did this rule make money in this window?” Drawdown answers “how much pain did it require?” Trade count answers “was it active enough to trust the sample?”</td></tr>
        <tr><th>What it does not prove</th><td>It does not predict tomorrow’s BTC price and it does not include every live-trading cost, outage, spread, key, or exchange failure mode.</td></tr>
      </tbody>
    </table>
    """


def _prediction_sentence(total_return: float, sharpe: float, trades: int) -> str:
    if trades <= 0:
        return "No trade fired in this window; the rule mostly stayed out."
    if total_return > 0 and sharpe > 0:
        return "Historically favorable in this window; candidate for paper trading, not live trading."
    if total_return > 0:
        return "Positive return but weak risk-adjusted quality; inspect drawdown before trusting it."
    return "Historically unfavorable in this window; do not automate without changing assumptions."


def _risk_sentence(max_drawdown: float, sharpe: float, trades: int) -> str:
    if trades < 5:
        return "thin sample"
    if max_drawdown <= -0.35:
        return "high drawdown"
    if sharpe <= 0:
        return "weak quality"
    if max_drawdown <= -0.15:
        return "moderate drawdown"
    return "controlled"


def _render_learning_before_automation() -> str:
    return """
    <div class="panel">
      <ol class="learning-list">
        <li><strong>Backtest first:</strong> learn OHLCV, fees, slippage, drawdown, Sharpe, and overfitting.</li>
        <li><strong>Paper trade next:</strong> run the same strategy without real money and compare live paper fills with backtest assumptions.</li>
        <li><strong>Risk controls:</strong> define max position, max daily loss, stop behavior, exchange failure handling, and kill switch.</li>
        <li><strong>Execution:</strong> understand market/limit orders, spread, liquidity, funding, API keys, IP allowlists, and no-withdrawal permissions.</li>
        <li><strong>Operations:</strong> monitor logs, alerts, heartbeat, data freshness, and reconciliation before considering live automation.</li>
      </ol>
      <div class="subtle">Use <span class="mono">/learn</span> and <span class="mono">docs/live-checklist.md</span> before moving from research to paper, and from paper to live.</div>
    </div>
    """


def _render_backtest_result(result: JsonDict | None) -> str:
    if result is None:
        return ""
    if result.get("ok") is not True:
        return (
            '<div class="panel" style="margin-top:14px">'
            '<span class="pill bad">failed</span> '
            f'<span class="mono">{_e(str(result.get("error", "unknown error")))}</span>'
            "</div>"
        )
    summary = result.get("summary")
    if not isinstance(summary, dict):
        return ""
    comparison = summary.get("comparison")
    if isinstance(comparison, dict):
        return _render_backtest_comparison_result(summary, comparison)
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    counts = summary.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    strategy = summary.get("strategy") or summary.get("engine_strategy") or "composite"
    total_return = _as_float(metrics.get("total_return"))
    sharpe = _as_float(metrics.get("sharpe"))
    trades = _as_int(counts.get("trades", summary.get("num_trades", 0)))
    return f"""
    <h2>Run Complete</h2>
    <div class="panel">
      <span class="pill good">published</span>
      <span class="mono">{_e(str(summary.get("run_id", "")))}</span>
      <div class="subtle" style="margin-top:8px">
        Prediction result: {_e(_prediction_sentence(total_return, sharpe, trades))}
      </div>
    </div>
    <div class="grid" style="margin-top:12px">
      {_card("Strategy", _e(str(strategy)))}
      {_card("Total Return", _fmt_pct(metrics.get("total_return")))}
      {_card("Max Drawdown", _fmt_pct(metrics.get("max_drawdown")))}
      {_card("Trades", _e(str(trades)))}
    </div>
    """


def _render_backtest_comparison_result(
    summary: Mapping[str, object],
    comparison: Mapping[str, object],
) -> str:
    ranked = comparison.get("ranked")
    rows: list[str] = []
    if isinstance(ranked, list):
        for raw in ranked:
            if not isinstance(raw, dict):
                continue
            strategy = str(raw.get("strategy", ""))
            rows.append(
                "<tr "
                f'data-strategy-rank="{_e(strategy)}">'
                f'<td>{_e(str(raw.get("rank", "")))}</td>'
                f'<td><strong>{_e(strategy)}</strong><br><span class="subtle">{_e(_strategy_label(strategy))}</span></td>'
                f'<td>{_fmt_pct(raw.get("total_return"))}</td>'
                f'<td>{_fmt_pct(raw.get("max_drawdown"))}</td>'
                f'<td>{_fmt_num(raw.get("sharpe"))}</td>'
                f'<td>{_e(str(raw.get("trades", 0)))}</td>'
                f'<td>{_e(str(raw.get("verdict", "")))}</td>'
                "</tr>"
            )
    recommendation = str(comparison.get("recommended", "No recommendation available."))
    return f"""
    <h2>Auto Comparison Complete</h2>
    <section class="panel">
      <span class="pill good">published</span>
      <span class="mono">{_e(str(summary.get("run_id", "")))}</span>
      <div class="subtle" style="margin-top:8px">
        Compare strategies on the same data before trusting one isolated backtest.
      </div>
      <div class="subtle" style="margin-top:12px">Recommended first paper candidate</div>
      <div class="metric" style="font-size:22px">{_e(recommendation)}</div>
    </section>
    <table style="margin-top:12px">
      <thead>
        <tr><th>Rank</th><th>Strategy</th><th>Total return</th><th>Max drawdown</th><th>Sharpe</th><th>Trades</th><th>Verdict</th></tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def _render_ohlcv_table(options: list[JsonDict]) -> str:
    if not options:
        return '<div class="panel subtle">No OHLCV parquet files found.</div>'
    rows = []
    for option in options:
        rows.append(
            "<tr>"
            f'<td class="mono">{_e(str(option.get("key", "")))}</td>'
            f"<td>{_e(str(option.get('rows', 0)))}</td>"
            f'<td class="mono">{_e(str(option.get("start", "")))}</td>'
            f'<td class="mono">{_e(str(option.get("end", "")))}</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Key</th><th>Rows</th><th>Start</th>"
        f"<th>End</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _render_intel_summary(intel: JsonDict) -> str:
    opportunities = intel.get("opportunities")
    if not isinstance(opportunities, list) or not opportunities:
        return (
            '<div class="panel subtle">No ranked opportunities yet. '
            'Start the intel scanner with <span class="mono">python scripts/run_all.py</span>.</div>'
        )
    top = opportunities[0] if isinstance(opportunities[0], dict) else {}
    kind = _e(str(top.get("kind", "unknown")))
    symbol = _e(str(top.get("symbol", "")))
    edge = _fmt_num(top.get("edge_bps"))
    score = _fmt_num(top.get("score"))
    generated = _e(str(intel.get("generated_at") or "unknown"))
    top_card = _card("Top", f"<span>{kind} {symbol}</span>")
    edge_card = _card("Edge", f'<span class="mono">{edge} bps</span>')
    score_card = _card("Score", f'<span class="mono">{score}</span>')
    generated_card = _card("Generated", f'<span class="mono">{generated}</span>')
    return (
        '<div class="grid">'
        f"{top_card}{edge_card}{score_card}{generated_card}"
        "</div>"
        '<div class="subtle" style="margin-top:8px">'
        f'<a href="/intel">open intelligence dashboard &rarr;</a> '
        f'({len(opportunities)} ranked candidate(s))</div>'
    )


def _render_intel_table(intel: JsonDict) -> str:
    opportunities = intel.get("opportunities")
    if not isinstance(opportunities, list) or not opportunities:
        return '<div class="panel subtle">No opportunities recorded yet.</div>'
    rows: list[str] = []
    for raw in opportunities:
        if not isinstance(raw, dict):
            continue
        rows.append(
            "<tr>"
            f'<td><span class="pill good">{_e(str(raw.get("kind", "")))}</span></td>'
            f'<td><strong>{_e(str(raw.get("symbol", "")))}</strong><br>'
            f'<span class="subtle">{_e(str(raw.get("venue", "")))}</span></td>'
            f'<td class="mono">{_fmt_num(raw.get("edge_bps"))}</td>'
            f'<td class="mono">{_fmt_num(raw.get("score"))}</td>'
            f'<td>{_e(str(raw.get("action", "")))}</td>'
            f'<td>{_e(str(raw.get("why", "")))}</td>'
            f'<td class="mono">{_fmt_num(raw.get("capacity_usd"))}</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Type</th><th>Market</th><th>Edge bps</th>"
        "<th>Score</th><th>Action</th><th>Why</th><th>Capacity USD</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_intel_page(context: DashboardContext) -> str:
    intel = _intel(context)
    generated = _e(str(intel.get("generated_at") or "not generated"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>QT - Intelligence</title>
  <style>
    :root {{ color-scheme: light; --bg:#f6f7f3; --ink:#15201b; --muted:#66736c; --line:#dce2dd;
              --accent:#0b7a75; --warn:#ad5a00; --bad:#a73737; --good:#197447;
              font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif; }}
    body {{ margin:0; background: var(--bg); color: var(--ink); }}
    main {{ width: min(1180px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 48px; }}
    header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-end; margin-bottom:18px; }}
    h1 {{ font-size: 24px; line-height:1.1; margin:0; }}
    a {{ color: var(--accent); }}
    .subtle {{ color: var(--muted); font-size: 14px; }}
    .panel {{ border:1px solid var(--line); background:#fff; border-radius:8px; padding:14px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th, td {{ border-bottom:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; font-size:13px; }}
    th {{ color: var(--muted); font-weight:700; background:#fbfcfa; }}
    .pill {{ display:inline-flex; border-radius:999px; padding:2px 8px; font-weight:700; font-size:12px; }}
    .good {{ color: var(--good); background:#e8f3ed; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    @media (max-width: 900px) {{ table {{ display:block; overflow-x:auto; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Intelligence</h1>
        <div class="subtle">Ranked funding, spread, basis, depeg, and wick candidates.</div>
      </div>
      <div class="subtle mono"><a href="/">back</a> · generated {generated}</div>
    </header>
    {_render_intel_table(intel)}
  </main>
</body>
</html>
"""


def _render_sources_table(sources: list[JsonDict]) -> str:
    rows = []
    for source in sources:
        configured = bool(source.get("configured"))
        exists = bool(source.get("exists"))
        fresh = source.get("fresh")
        if fresh is True:
            status = '<span class="pill good">fresh</span>'
        elif exists:
            status = '<span class="pill warn">stale</span>'
        elif configured:
            status = '<span class="pill warn">missing</span>'
        else:
            status = '<span class="pill muted">needs key</span>'
        rows.append(
            "<tr>"
            f"<td>{status}</td>"
            f"<td><strong>{_e(str(source.get('name', '')))}</strong><br>"
            f"<span class=\"subtle\">{_e(str(source.get('provider', '')))}</span></td>"
            f"<td>{_e(str(source.get('category', '')))}</td>"
            f"<td>{_e(str(source.get('used_for', '')))}</td>"
            f"<td class=\"mono\">{_e(str(source.get('dataset', '')))} / {_e(str(source.get('key', '')))}</td>"
            f"<td>{_e(str(source.get('rows', 0)))}<br><span class=\"subtle\">{_e(str(source.get('end') or 'no data'))}</span></td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Status</th><th>Source</th><th>Group</th>"
        "<th>How It Is Used</th><th>Store Key</th><th>Rows / Last Seen</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_strategies_table(strategies: list[JsonDict]) -> str:
    if not strategies:
        return (
            '<div class="panel subtle">No strategies running yet. '
            'Start them with <span class="mono">python scripts/run_all.py</span>.</div>'
        )
    rows: list[str] = []
    for snap in strategies:
        name = str(snap.get("name", "?"))
        status = str(snap.get("status", "unknown"))
        cls = "good" if status == "healthy" else "warn" if status == "degraded" else "bad" if status in {"failed", "stopped"} else "muted"
        details = snap.get("details") or {}
        opp = details.get("last_opportunity") if isinstance(details, dict) else None
        last_eval = details.get("last_evaluation") if isinstance(details, dict) else None
        description = details.get("description", "") if isinstance(details, dict) else ""
        opp_html = "<span class=\"subtle\">no opportunity yet</span>"
        if isinstance(opp, dict):
            opp_html = (
                f'<span class="pill good">{_e(str(opp.get("action", "")))}</span> '
                f'<span class="mono">{_e(str(opp.get("reason", "")))}</span><br>'
                f'<span class="subtle">{_e(str(opp.get("ts", "")))}</span>'
            )
        metrics_html = ""
        if isinstance(last_eval, dict):
            m = last_eval.get("metrics") or {}
            if isinstance(m, dict) and m:
                top = ", ".join(f"{k}={m[k]}" for k in list(m)[:4])
                metrics_html = f'<br><span class="subtle mono">{_e(top)}</span>'
        rows.append(
            "<tr>"
            f'<td><span class="pill {cls}">{_e(status)}</span></td>'
            f'<td><a href="/strategy/{_e(name)}"><strong>{_e(name)}</strong></a><br>'
            f'<span class="subtle">{_e(str(description))}</span>{metrics_html}</td>'
            f"<td>{_e(str(snap.get('cycle', 0)))}</td>"
            f"<td>{_e(str(snap.get('updated_at', '')))}</td>"
            f"<td>{opp_html}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Status</th><th>Strategy</th>"
        "<th>Cycle</th><th>Updated</th><th>Last Opportunity</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_strategy_detail(name: str, snap: JsonDict, pf: JsonDict | None = None) -> str:
    status = str(snap.get("status", "unknown"))
    cls = "good" if status == "healthy" else "warn" if status == "degraded" else "bad" if status in {"failed", "stopped"} else "muted"
    details = snap.get("details") or {}
    opp = details.get("last_opportunity") if isinstance(details, dict) else None
    last_eval = details.get("last_evaluation") if isinstance(details, dict) else None
    params = details.get("params") if isinstance(details, dict) else None
    description = details.get("description", "") if isinstance(details, dict) else ""
    metrics = (
        last_eval.get("metrics") if isinstance(last_eval, dict) else None
    ) or {}
    pnl_html = ""
    if pf is not None:
        equity = _as_float(pf.get("last_equity"))
        realized = _as_float(pf.get("realized_pnl"))
        cash = _as_float(pf.get("cash"))
        num_trades = _as_int(pf.get("num_trades"))
        rcls = _pnl_class(realized)
        pc_equity = _card("Equity", f'<span class="mono">{_fmt_money(equity)}</span>')
        pc_realized = _card("Realized P&L", f'<span class="pill {rcls}">{_fmt_money(realized)}</span>')
        pc_cash = _card("Cash", f'<span class="mono">{_fmt_money(cash)}</span>')
        pc_trades = _card("Trades", _e(str(num_trades)))
        pnl_html = (
            '<h2>Portfolio P&amp;L</h2>'
            '<div class="grid">'
            f"{pc_equity}{pc_realized}{pc_cash}{pc_trades}"
            "</div>"
            '<div class="subtle" style="margin-top:6px">Full account book on the '
            '<a href="/portfolio">P&amp;L page</a>.</div>'
        )
    opp_html = '<div class="panel subtle">No opportunity has fired yet.</div>'
    if isinstance(opp, dict):
        opp_html = (
            '<div class="panel">'
            f'<div><span class="pill good">{_e(str(opp.get("action", "")))}</span> '
            f'<strong>{_e(str(opp.get("reason", "")))}</strong></div>'
            f'<div class="subtle" style="margin-top:6px">{_e(str(opp.get("ts", "")))}</div>'
            f'<div class="mono" style="margin-top:10px">{_e(json.dumps(opp.get("details") or {}, sort_keys=True, default=str))}</div>'
            '</div>'
        )
    metrics_html = (
        f'<div class="mono">{_e(json.dumps(metrics, indent=2, sort_keys=True, default=str))}</div>'
        if isinstance(metrics, dict) else '<div class="subtle">no metrics yet</div>'
    )
    params_html = (
        f'<div class="mono">{_e(json.dumps(params, indent=2, sort_keys=True, default=str))}</div>'
        if isinstance(params, dict) else '<div class="subtle">no params reported</div>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>QT — {_e(name)}</title>
  <style>
    :root {{ color-scheme: light; --bg:#f6f7f3; --ink:#15201b; --muted:#66736c; --line:#dce2dd;
              --accent:#0b7a75; --warn:#ad5a00; --bad:#a73737; --good:#197447;
              font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif; }}
    body {{ margin:0; background: var(--bg); color: var(--ink); }}
    main {{ width: min(960px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 48px; }}
    header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-end; margin-bottom:18px; }}
    h1 {{ font-size: 24px; line-height:1.1; margin:0; }}
    h2 {{ font-size: 16px; margin: 24px 0 10px; }}
    a {{ color: var(--accent); }}
    .subtle {{ color: var(--muted); font-size: 14px; }}
    .panel {{ border:1px solid var(--line); background:#fff; border-radius:8px; padding:14px; }}
    .pill {{ display:inline-flex; border-radius:999px; padding:2px 8px; font-weight:700; font-size:12px; }}
    .good {{ color: var(--good); background:#e8f3ed; }}
    .warn {{ color: var(--warn); background:#fff0dd; }}
    .bad  {{ color: var(--bad); background:#f8e7e7; }}
    .muted {{ color: var(--muted); background:#eef1ee; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>{_e(name)} <span class="pill {cls}">{_e(status)}</span></h1>
        <div class="subtle">{_e(str(description))}</div>
      </div>
      <div class="subtle mono"><a href="/">← back</a> · <a href="/portfolio">P&amp;L</a> · refreshes 60s</div>
    </header>
    <div class="panel">
      <div class="subtle">cycle</div><div class="mono">{_e(str(snap.get("cycle", 0)))}</div>
      <div class="subtle" style="margin-top:8px">updated</div><div class="mono">{_e(str(snap.get("updated_at", "")))}</div>
      <div class="subtle" style="margin-top:8px">last error</div><div class="mono">{_e(str(snap.get("last_error") or "none"))}</div>
    </div>
    {pnl_html}
    <h2>Last Opportunity</h2>
    {opp_html}
    <h2>Latest Metrics</h2>
    <div class="panel">{metrics_html}</div>
    <h2>Configured Params</h2>
    <div class="panel">{params_html}</div>
  </main>
</body>
</html>
"""


def _pnl_class(value: float) -> str:
    return "good" if value > 0 else "bad" if value < 0 else "muted"


def _fmt_money(value: object) -> str:
    if not isinstance(value, int | float):
        return "n/a"
    return f"{value:,.2f}"


def _render_portfolio_summary(portfolios: list[JsonDict]) -> str:
    """Compact P&L table for the home page."""
    if not portfolios:
        return (
            '<div class="panel subtle">No paper trades yet. Strategies write a '
            'ledger under <span class="mono">data/runtime/portfolios/</span> '
            'once they execute their first opportunity.</div>'
        )
    rows: list[str] = []
    total_equity = 0.0
    total_realized = 0.0
    for pf in portfolios:
        name = str(pf.get("name", "?"))
        equity = _as_float(pf.get("last_equity"))
        realized = _as_float(pf.get("realized_pnl"))
        cash = _as_float(pf.get("cash"))
        num_trades = _as_int(pf.get("num_trades"))
        total_equity += equity
        total_realized += realized
        rcls = _pnl_class(realized)
        rows.append(
            "<tr>"
            f'<td><a href="/strategy/{_e(name)}"><strong>{_e(name)}</strong></a></td>'
            f'<td class="mono">{_fmt_money(equity)}</td>'
            f'<td class="mono">{_fmt_money(cash)}</td>'
            f'<td class="mono"><span class="pill {rcls}">{_fmt_money(realized)}</span></td>'
            f"<td>{num_trades}</td>"
            "</tr>"
        )
    tcls = _pnl_class(total_realized)
    c_equity = _card("Total Equity", f'<span class="mono">{_fmt_money(total_equity)}</span>')
    c_realized = _card("Total Realized P&L", f'<span class="pill {tcls}">{_fmt_money(total_realized)}</span>')
    c_count = _card("Strategies Trading", _e(str(len(portfolios))))
    c_detail = _card("View Detail", '<a href="/portfolio">open &rarr;</a>')
    return (
        '<div class="grid">'
        f"{c_equity}{c_realized}{c_count}{c_detail}"
        "</div>"
        '<table style="margin-top:12px"><thead><tr><th>Strategy</th><th>Equity</th>'
        "<th>Cash</th><th>Realized P&amp;L</th><th>Trades</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_portfolio_overview(context: DashboardContext) -> str:
    """Full /portfolio page: per-strategy P&L + recent trades."""
    portfolios = _portfolios(context)
    blocks: list[str] = []
    for pf in portfolios:
        blocks.append(_render_portfolio_block(pf))
    body = "".join(blocks) if blocks else (
        '<div class="panel subtle">No paper trades recorded yet.</div>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>QT — Portfolio P&amp;L</title>
  <style>
    :root {{ color-scheme: light; --bg:#f6f7f3; --ink:#15201b; --muted:#66736c; --line:#dce2dd;
              --accent:#0b7a75; --warn:#ad5a00; --bad:#a73737; --good:#197447;
              font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif; }}
    body {{ margin:0; background: var(--bg); color: var(--ink); }}
    main {{ width: min(1080px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 48px; }}
    header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-end; margin-bottom:18px; }}
    h1 {{ font-size: 24px; line-height:1.1; margin:0; }}
    h2 {{ font-size: 16px; margin: 24px 0 10px; }}
    a {{ color: var(--accent); }}
    .subtle {{ color: var(--muted); font-size: 14px; }}
    .grid {{ display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:12px; }}
    .panel {{ border:1px solid var(--line); background:#fff; border-radius:8px; padding:14px; }}
    .metric {{ font-size:22px; font-weight:700; margin-top:6px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th, td {{ border-bottom:1px solid var(--line); padding:9px; text-align:left; font-size:13px; }}
    th {{ color: var(--muted); font-weight:700; background:#fbfcfa; }}
    .pill {{ display:inline-flex; border-radius:999px; padding:2px 8px; font-weight:700; font-size:12px; }}
    .good {{ color: var(--good); background:#e8f3ed; }}
    .warn {{ color: var(--warn); background:#fff0dd; }}
    .bad  {{ color: var(--bad); background:#f8e7e7; }}
    .muted {{ color: var(--muted); background:#eef1ee; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: repeat(2,minmax(0,1fr)); }} table {{ display:block; overflow-x:auto; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Portfolio P&amp;L</h1>
        <div class="subtle">Paper-trading account book per strategy. Are we making money?</div>
      </div>
      <div class="subtle mono"><a href="/">← back</a> · refreshes 60s</div>
    </header>
    {body}
  </main>
</body>
</html>
"""


def _render_portfolio_block(pf: JsonDict) -> str:
    name = str(pf.get("name", "?"))
    equity = _as_float(pf.get("last_equity"))
    realized = _as_float(pf.get("realized_pnl"))
    cash = _as_float(pf.get("cash"))
    fees = _as_float(pf.get("total_fees"))
    peak = _as_float(pf.get("equity_peak"))
    dd = ((equity - peak) / peak) if peak > 0 else 0.0
    positions = pf.get("positions") or {}
    trades = pf.get("trades") or []
    rcls = _pnl_class(realized)

    pos_html = "<span class=\"subtle\">flat (no open position)</span>"
    if isinstance(positions, dict) and positions:
        parts = []
        avg = pf.get("avg_price") or {}
        for sym, qty in positions.items():
            if isinstance(qty, int | float) and qty > 1e-12:
                ap = avg.get(sym, 0.0) if isinstance(avg, dict) else 0.0
                parts.append(f'{_e(sym)}: {qty:.6f} @ {_fmt_money(ap)}')
        if parts:
            pos_html = '<span class="mono">' + "; ".join(parts) + "</span>"

    trade_rows = ""
    if isinstance(trades, list) and trades:
        recent = trades[-15:][::-1]
        for t in recent:
            if not isinstance(t, dict):
                continue
            side = str(t.get("side", ""))
            scls = "good" if side == "buy" else "warn"
            trade_rows += (
                "<tr>"
                f'<td class="subtle">{_e(str(t.get("ts", "")))}</td>'
                f'<td><span class="pill {scls}">{_e(side)}</span></td>'
                f'<td class="mono">{_fmt_num_generic(t.get("qty"))}</td>'
                f'<td class="mono">{_fmt_money(t.get("price"))}</td>'
                f'<td class="mono">{_fmt_money(t.get("fee"))}</td>'
                f'<td class="mono">{_fmt_money(t.get("equity"))}</td>'
                "</tr>"
            )
    trades_table = (
        '<table style="margin-top:10px"><thead><tr><th>Time</th><th>Side</th>'
        "<th>Qty</th><th>Price</th><th>Fee</th><th>Equity After</th></tr></thead>"
        f"<tbody>{trade_rows}</tbody></table>"
        if trade_rows else '<div class="subtle" style="margin-top:8px">No trades yet.</div>'
    )

    c_equity = _card("Equity", f'<span class="mono">{_fmt_money(equity)}</span>')
    c_realized = _card("Realized P&L", f'<span class="pill {rcls}">{_fmt_money(realized)}</span>')
    c_cash = _card("Cash", f'<span class="mono">{_fmt_money(cash)}</span>')
    c_dd = _card("Drawdown", f'<span class="mono">{dd:.2%}</span>')
    return (
        f'<h2><a href="/strategy/{_e(name)}">{_e(name)}</a></h2>'
        '<div class="grid">'
        f"{c_equity}{c_realized}{c_cash}{c_dd}"
        "</div>"
        f'<div class="subtle" style="margin-top:8px">Open position: {pos_html} '
        f'&middot; total fees paid: <span class="mono">{_fmt_money(fees)}</span></div>'
        f"{trades_table}"
    )


def _fmt_num_generic(value: object) -> str:
    if not isinstance(value, int | float):
        return _e(str(value)) if value is not None else "n/a"
    return f"{value:.6f}"


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_int(value: object, default: int = 0) -> int:
    return int(_as_float(value, float(default)))


def _card(label: str, value: str) -> str:
    return f'<div class="panel"><div class="subtle">{_e(label)}</div><div class="metric">{value}</div></div>'


def _fmt_pct(value: object) -> str:
    if not isinstance(value, int | float):
        return "n/a"
    return f"{value:.2%}"


def _fmt_num(value: object) -> str:
    if not isinstance(value, int | float):
        return "n/a"
    if not math.isfinite(float(value)):
        return "n/a"
    return f"{value:.2f}"


def _e(value: str) -> str:
    return html.escape(value, quote=True)
