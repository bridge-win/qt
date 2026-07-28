"""Dependency-free local dashboard server."""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypeAlias
from urllib.parse import parse_qs, urlparse

import pandas as pd

from qt.backtest.artifacts import latest_backtest_summary, write_backtest_artifacts
from qt.backtest.engine import Backtester
from qt.backtest.strategy_backtest import (
    SUPPORTED,
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

JsonDict: TypeAlias = dict[str, object]
_BACKTEST_STRATEGIES = ("composite", *SUPPORTED)


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

        def _send_json(self, body: JsonDict) -> None:
            payload = json.dumps(body, indent=2, sort_keys=True, default=str).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return DashboardHandler


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
    strategy = _form_value(fields, "strategy", "composite")
    ohlcv_key = _form_value(fields, "ohlcv_key", _default_ohlcv_key(context))
    initial_cash_raw = _form_value(fields, "initial_cash", "100000")
    if strategy not in _BACKTEST_STRATEGIES:
        return {"ok": False, "error": f"unknown strategy: {strategy}"}
    all_options = _ohlcv_options(context)
    all_keys = {str(item.get("key")) for item in all_options}
    available_keys = {
        str(item.get("key"))
        for item in all_options
        if _as_int(item.get("rows")) > 0
    }
    if ohlcv_key not in all_keys:
        return {"ok": False, "error": f"unknown OHLCV key: {ohlcv_key}"}
    if ohlcv_key not in available_keys:
        return {
            "ok": False,
            "error": (
                f"unavailable OHLCV key: {ohlcv_key} has no rows; "
                "choose a listed non-empty data source"
            ),
        }
    try:
        initial_cash = float(initial_cash_raw)
    except ValueError:
        return {"ok": False, "error": "initial cash must be numeric"}
    if initial_cash <= 0:
        return {"ok": False, "error": "initial cash must be positive"}

    store = ParquetStore(context.parquet_dir)
    ohlcv = store.read("ohlcv", ohlcv_key)
    if ohlcv.empty:
        return {"ok": False, "error": f"no OHLCV rows for {ohlcv_key}"}

    try:
        if strategy == "composite":
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
        return {"ok": False, "error": str(error)}
    return {"ok": True, "summary": summary}


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
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


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
    selected_key = _default_ohlcv_key(context)
    result_html = _render_backtest_result(result)
    strategy_options = "".join(
        f'<option value="{_e(strategy)}">{_e(strategy)}</option>'
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
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QT — Backtest</title>
  <style>
    :root {{ color-scheme: light; --bg:#f6f7f3; --ink:#15201b; --muted:#66736c; --line:#dce2dd;
              --accent:#0b7a75; --warn:#ad5a00; --bad:#a73737; --good:#197447;
              font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif; }}
    body {{ margin:0; background: var(--bg); color: var(--ink); }}
    main {{ width:min(1080px, calc(100vw - 32px)); margin:0 auto; padding:28px 0 48px; }}
    header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-end; margin-bottom:22px; }}
    h1 {{ font-size:28px; line-height:1.1; margin:0; }}
    h2 {{ font-size:17px; margin:28px 0 10px; }}
    a {{ color:var(--accent); }}
    form {{ display:grid; grid-template-columns: 1.2fr 1.5fr 1fr auto; gap:12px; align-items:end; }}
    label {{ display:grid; gap:6px; color:var(--muted); font-size:13px; font-weight:700; }}
    select, input {{ font:inherit; color:var(--ink); background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px 11px; }}
    button {{ font:inherit; font-weight:700; color:#fff; background:var(--accent); border:0; border-radius:8px; padding:11px 14px; cursor:pointer; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th, td {{ border-bottom:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; font-size:13px; }}
    th {{ color:var(--muted); font-weight:700; background:#fbfcfa; }}
    tr:last-child td {{ border-bottom:0; }}
    .subtle {{ color:var(--muted); font-size:14px; }}
    .panel {{ border:1px solid var(--line); background:#fff; border-radius:8px; padding:14px; }}
    .grid {{ display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:12px; }}
    .metric {{ font-size:24px; font-weight:700; margin-top:6px; }}
    .pill {{ display:inline-flex; border-radius:999px; padding:2px 8px; font-weight:700; font-size:12px; }}
    .good {{ color:var(--good); background:#e8f3ed; }}
    .bad {{ color:var(--bad); background:#f8e7e7; }}
    .mono {{ font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    @media (max-width: 900px) {{ header {{ display:block; }} form, .grid {{ grid-template-columns:1fr; }} table {{ display:block; overflow-x:auto; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Backtest</h1>
        <div class="subtle">Run controlled local-data replays and publish the result to the monitor.</div>
      </div>
      <div class="subtle mono"><a href="/">← monitor</a> · <a href="/api/backtests/latest">latest JSON</a></div>
    </header>
    <section class="panel">
      <form method="post" action="/backtest/run">
        <label>Strategy
          <select name="strategy">{strategy_options}</select>
        </label>
        <label>Data source
          <select name="ohlcv_key">{data_options}</select>
        </label>
        <label>Initial cash
          <input name="initial_cash" value="100000" inputmode="decimal">
        </label>
        <button type="submit"{submit_attrs}>Run backtest</button>
      </form>
      <div class="subtle" style="margin-top:10px">
        <span class="mono">composite</span> uses QT's threshold/risk engine.
        <span class="mono">dca</span>, <span class="mono">trend</span>,
        <span class="mono">carry</span>, and <span class="mono">wick</span>
        use the independent BTC backtest engine through QT's adapter.
      </div>
    </section>
    {result_html}
    <h2>Latest Published Result</h2>
    {_render_backtest(latest)}
    <h2>Available OHLCV Data</h2>
    {_render_ohlcv_table(all_options)}
  </main>
</body>
</html>
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
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    counts = summary.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    strategy = summary.get("strategy") or summary.get("engine_strategy") or "composite"
    return f"""
    <h2>Run Complete</h2>
    <div class="panel">
      <span class="pill good">published</span>
      <span class="mono">{_e(str(summary.get("run_id", "")))}</span>
    </div>
    <div class="grid" style="margin-top:12px">
      {_card("Strategy", _e(str(strategy)))}
      {_card("Total Return", _fmt_pct(metrics.get("total_return")))}
      {_card("Max Drawdown", _fmt_pct(metrics.get("max_drawdown")))}
      {_card("Trades", _e(str(counts.get("trades", summary.get("num_trades", 0)))))}
    </div>
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
    return f"{value:.2f}"


def _e(value: str) -> str:
    return html.escape(value, quote=True)
