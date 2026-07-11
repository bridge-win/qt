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
from urllib.parse import urlparse

from qt.backtest.artifacts import latest_backtest_summary
from qt.data.catalog import data_source_statuses
from qt.data.store import ParquetStore
from qt.intel.ranker import read_opportunities
from qt.monitoring.state import MonitorStateStore
from qt.portfolio import read_all_portfolios, read_portfolio

JsonDict: TypeAlias = dict[str, object]


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
            if path == "/api/sources":
                self._send_json({"sources": _sources(context)})
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
            if path == "/intel":
                self._send_html(_render_intel_page(context))
                return
            if path == "/portfolio":
                self._send_html(_render_portfolio_overview(context))
                return
            if path == "/learn":
                self._send_html(_render_learn_page(context))
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

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send_html(self, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
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


# --------------------------------------------------------------------------
# Shared page shell
#
# Every full HTML page used to duplicate its own <head>/<style> block. That
# shell now lives in one place so a design tweak (or a new nav link) is a
# single edit and every page stays visually consistent.
# --------------------------------------------------------------------------

_PAGE_STYLE = """
    :root {
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
    }
    body { margin: 0; background: var(--bg); color: var(--ink); }
    main { margin: 0 auto; padding: 28px 0 48px; }
    header { display: flex; justify-content: space-between; gap: 20px; align-items: flex-end; margin-bottom: 22px; }
    h1 { font-size: 28px; line-height: 1.1; margin: 0; letter-spacing: 0; }
    h2 { font-size: 18px; margin: 28px 0 12px; letter-spacing: 0; }
    h3 { font-size: 15px; margin: 16px 0 6px; letter-spacing: 0; }
    a { color: var(--accent); }
    nav.top { display: flex; gap: 14px; font-size: 13px; margin-top: 6px; flex-wrap: wrap; }
    nav.top a { text-decoration: none; }
    nav.top a.here { font-weight: 700; color: var(--ink); }
    .subtle { color: var(--muted); font-size: 14px; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .panel { border: 1px solid var(--line); background: #fff; border-radius: 8px; padding: 14px; }
    .metric { font-size: 24px; font-weight: 700; margin-top: 6px; }
    table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
    th, td { border-bottom: 1px solid var(--line); padding: 10px; text-align: left; vertical-align: top; font-size: 13px; }
    th { color: var(--muted); font-weight: 700; background: #fbfcfa; }
    tr:last-child td { border-bottom: 0; }
    .pill { display: inline-flex; border-radius: 999px; padding: 2px 8px; font-weight: 700; font-size: 12px; }
    .tag { display: inline-flex; border-radius: 6px; padding: 1px 7px; font-size: 12px; font-weight: 700;
           background: #e8f3ed; color: var(--accent); margin-right: 6px; }
    .good { color: var(--good); background: #e8f3ed; }
    .warn { color: var(--warn); background: #fff0dd; }
    .bad { color: var(--bad); background: #f8e7e7; }
    .muted { color: var(--muted); background: #eef1ee; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .prose { line-height: 1.5; font-size: 14px; }
    .prose p { margin: 8px 0; }
    .prose ul { margin: 6px 0; padding-left: 18px; }
    .prose li { margin: 3px 0; }
    .prose code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
                  background: #eef1ee; border-radius: 4px; padding: 1px 5px; font-size: 12px; }
    .cite { color: var(--muted); font-size: 13px; }
    ol.refs { font-size: 13px; line-height: 1.5; padding-left: 20px; }
    ol.refs li { margin: 5px 0; }
    @media (max-width: 900px) {
      header { display: block; }
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .cards { grid-template-columns: 1fr; }
      table { display: block; overflow-x: auto; }
    }
    @media (max-width: 560px) {
      main { padding-top: 18px; }
      .grid { grid-template-columns: 1fr; }
      th, td { white-space: nowrap; }
    }
"""

_NAV_LINKS: tuple[tuple[str, str], ...] = (
    ("/", "Monitor"),
    ("/portfolio", "P&amp;L"),
    ("/intel", "Intelligence"),
    ("/learn", "Learn Quant"),
)


def _top_nav(current: str) -> str:
    items = []
    for href, label in _NAV_LINKS:
        cls = ' class="here"' if href == current else ""
        items.append(f'<a href="{href}"{cls}>{label}</a>')
    return '<nav class="top">' + "".join(items) + "</nav>"


def _page(
    *,
    title: str,
    heading: str,
    subtitle: str,
    body: str,
    current: str,
    aside: str = "",
    max_width: int = 1180,
    refresh: int = 60,
) -> str:
    """Render a full HTML document with the shared shell, header, and nav."""
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    aside_html = f'<div class="subtle mono">{aside}</div>' if aside else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_tag}
  <title>{title}</title>
  <style>{_PAGE_STYLE}
    main {{ width: min({max_width}px, calc(100vw - 32px)); }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>{heading}</h1>
        <div class="subtle">{subtitle}</div>
        {_top_nav(current)}
      </div>
      {aside_html}
    </header>
    {body}
  </main>
</body>
</html>
"""


def _render_home(context: DashboardContext) -> str:
    sources = _sources(context)
    backtest = _latest_backtest(context)
    monitor = _monitor(context)
    strategies = _strategies(context)
    portfolios = _portfolios(context)
    intel = _intel(context)
    body = f"""{_render_plain_banner(strategies, portfolios)}
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
    {_render_sources_table(sources)}"""
    return _page(
        title="QT Monitor",
        heading="QT Monitor",
        subtitle="Data coverage, live heartbeat, P&amp;L, and latest backtest artifacts.",
        body=body,
        current="/",
        aside="refreshes every 60s",
    )


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
        return '<div class="panel subtle">No exported backtest yet. Run `qt backtest` first.</div>'
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
    return _page(
        title="QT - Intelligence",
        heading="Intelligence",
        subtitle="Ranked funding, spread, basis, depeg, and wick candidates.",
        body=_render_intel_table(intel),
        current="/intel",
        aside=f"generated {generated}",
    )


# --------------------------------------------------------------------------
# Learn Quant page
#
# A summarized, citation-backed curriculum for building quantitative and
# financial knowledge from zero. The full companion lives in docs/LEARNING.md;
# the content below is the condensed on-dashboard version. It is kept as data
# so it stays easy to edit and can be unit-tested without HTML parsing.
# --------------------------------------------------------------------------

# Each layer: (id, title, why, concepts, where, how, repo_ref)
_LEARN_LAYERS: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (
        "L1", "Mathematics & statistics",
        "Every model is applied probability. If you can't derive why a −2 "
        "Z-score is a ~2.3% tail event, you can't read a funding signal.",
        "Probability &amp; Bayes · estimators &amp; hypothesis testing · linear "
        "algebra (PCA, regression) · optimization · stochastic processes.",
        "Wasserman, <em>All of Statistics</em> [2]; Ross [3]; MIT OCW 18.05 "
        "[16]; 3Blue1Brown &amp; StatQuest for intuition.",
        "Re-derive the Sharpe ratio, Z-score and OLS on paper, then check "
        "them in numpy.",
        "src/qt/backtest/metrics.py",
    ),
    (
        "L2", "Programming &amp; data engineering",
        "Alpha decays; infrastructure compounds. Most quant work is data "
        "plumbing and avoiding look-ahead bias.",
        "numpy/pandas &amp; vectorization · data hygiene (survivorship &amp; "
        "look-ahead bias, point-in-time) · reproducible, replayable pipelines · "
        "tests, typing, logging.",
        "McKinney, <em>Python for Data Analysis</em> [5]; MIT OCW 6.0001 [17]; "
        "QuantConnect/Lean docs [19].",
        "Rebuild one indicator yourself and diff your output against this "
        "repo's — the bugs you find are the lesson.",
        "src/qt/data/ (ParquetStore replay store)",
    ),
    (
        "L3", "Financial markets &amp; instruments",
        "Know <em>what</em> you trade before <em>how</em>. A funding rate is "
        "noise until you understand perpetual swaps.",
        "Market structure, makers/takers, fees · spot vs. futures vs. perps "
        "(funding!) vs. options (Greeks) · crypto: on-chain metrics, "
        "liquidations, stablecoin pegs · log vs. simple returns.",
        "Hull, <em>Options, Futures &amp; Other Derivatives</em> [6]; Columbia "
        "FE&amp;RM on Coursera [18]; Glassnode Academy; MIT 15.401 [16].",
        "Open a paper account; watch a funding payment settle and a "
        "liquidation cascade next to the order book.",
        "src/qt/intel/scanners.py",
    ),
    (
        "L4", "Time-series &amp; econometrics",
        "Prices have fat tails, volatility clustering and regime changes; "
        "i.i.d. statistics quietly break.",
        "Stationarity, autocorrelation, cointegration · GARCH volatility &amp; "
        "regime-switching · realized vs. implied vol · Extreme Value Theory · "
        "spurious-regression traps.",
        "Tsay, <em>Analysis of Financial Time Series</em> [7]; Hyndman, "
        "<em>Forecasting: Principles &amp; Practice</em> (free) [20].",
        "Fit a GARCH(1,1) to BTC returns; compare its conditional vol to this "
        "repo's cheap short/long vol-ratio regime signal.",
        "src/qt/indicators/volatility.py",
    ),
    (
        "L5", "Signal / factor / alpha construction",
        "Where the edge is supposed to live — and where people fool "
        "themselves most. Turn a noisy idea into a falsifiable factor.",
        "Factor investing (value, momentum, carry) · signal combination: "
        "weighted Z-scores vs. N-of-K voting · Fundamental Law "
        "<code>IR ≈ IC·√breadth</code> · leakage &amp; meta-labeling.",
        "López de Prado, <em>Advances in Financial ML</em> [1]; Grinold &amp; "
        "Kahn [8]; Narang, <em>Inside the Black Box</em> [9]; Quantopian "
        "Lecture Series.",
        "Test one factor's information coefficient honestly, out-of-sample, "
        "before you even think about adding a second.",
        "src/qt/indicators/composite.py · src/qt/signal/engine.py",
    ),
    (
        "L6", "Portfolio construction &amp; risk",
        "You survive on risk management, not on being right. Sizing and "
        "drawdown control separate compounding from ruin.",
        "Mean-variance &amp; the efficient frontier · Kelly criterion and why "
        "practitioners use <em>fractional</em> Kelly · vol targeting, risk "
        "parity · drawdown, VaR/Expected Shortfall, kill-switch.",
        "Grinold &amp; Kahn [8]; Kelly's original paper [21] &amp; Thorp; "
        "Ilmanen, <em>Expected Returns</em>.",
        "Simulate the same edge at full/half/quarter Kelly across random "
        "paths; watch median growth vs. ruin probability.",
        "src/qt/risk/sizing.py · src/qt/risk/engine.py",
    ),
    (
        "L7", "Backtesting &amp; validation",
        "A backtest is a hypothesis test that is trivially easy to rig — "
        "usually by accident. This layer is what makes a result trustworthy.",
        "Look-ahead/survivorship bias, p-hacking · walk-forward &amp; "
        "out-of-sample · Deflated Sharpe &amp; number-of-trials · "
        "multiple-testing (demand t≈3) · Monte Carlo of the equity curve.",
        "López de Prado [1]; Bailey et al., <em>Pseudo-Mathematics &amp; "
        "Financial Charlatanism</em> [23]; Chan, <em>Quantitative Trading</em> "
        "[24].",
        "Deliberately overfit once (search 1,000 param combos, pick the best), "
        "then watch it die out-of-sample. You'll recognize it forever.",
        "src/qt/backtest/walkforward.py · montecarlo.py",
    ),
    (
        "L8", "Execution &amp; microstructure",
        "The gap between backtest and reality is mostly execution: spread, "
        "slippage, fees, latency and market impact.",
        "Order-book mechanics, maker/taker fees · slippage &amp; market impact "
        "(Almgren–Chriss [25]) · adverse selection (Kyle [26]) · capacity — "
        "why an edge dies at size.",
        "O'Hara, <em>Market Microstructure Theory</em>; Harris, <em>Trading "
        "&amp; Exchanges</em>; the two canonical papers [25][26].",
        "Add realistic fees and slippage to a &quot;winning&quot; backtest and "
        "watch the Sharpe fall — that delta is the lesson.",
        "src/qt/backtest/fills.py · src/qt/intel/ranker.py",
    ),
    (
        "L9", "Behavioral finance &amp; practitioner wisdom",
        "Edges often exist because others behave predictably badly — "
        "panic-selling into capitulation is this whole strategy's thesis.",
        "Biases: overconfidence, recency, loss aversion, herding · why forced "
        "liquidations create mean reversion · reflexivity &amp; edge decay · "
        "managing your own psychology.",
        "Kahneman, <em>Thinking, Fast and Slow</em> [14]; Taleb [13]; Duke, "
        "<em>Thinking in Bets</em> [15]; Shiller, <em>Irrational Exuberance</em>.",
        "Journal your emotional state next to each paper trade; correlate your "
        "worst decisions with your emotional peaks.",
        "docs/strategy.md (the thesis itself)",
    ),
)

# Plain-language glossary for absolute beginners: (term, meaning)
_LEARN_GLOSSARY: tuple[tuple[str, str], ...] = (
    ("Spot 现货", "Buying the actual coin. You pay $100, you own $100 of BTC."),
    ("Perpetual / perp 永续合约", "A bet on the price that never expires. You "
     "don't own the coin — you hold a contract that tracks its price."),
    ("Long / short 做多 / 做空", "Long = you profit if price rises. "
     "Short = you profit if price falls."),
    ("Leverage 杠杆", "Borrowed money to trade bigger than your capital. 10x "
     "leverage turns a 10% move against you into a 100% loss."),
    ("Liquidation 爆仓", "When a leveraged position loses so much the exchange "
     "force-closes it. Cascades of these cause flash crashes."),
    ("Funding rate 资金费率", "A small payment (every 1–8h) between longs and "
     "shorts on perps that keeps the perp price near spot. Usually longs pay "
     "shorts — that payment is the carry trade's income."),
    ("Limit / market order 限价单 / 市价单", "Limit: buy only at your chosen "
     "price or better. Market: buy right now at whatever price is available."),
    ("Drawdown 回撤", "How far your account has fallen from its peak. The "
     "number that decides whether you survive."),
    ("Sharpe ratio 夏普比率", "Return per unit of risk taken. 1 is decent, 2 "
     "is very good, 5+ in a backtest usually means a bug or overfitting."),
    ("Paper trading 纸面交易", "Trading with fake money to test a strategy. "
     "This whole system runs paper-only until you deliberately enable live."),
)

# Beginner path — the first steps for someone with little knowledge:
# (step, action, why)
_LEARN_START: tuple[tuple[str, str, str], ...] = (
    ("1", "Run <code>./start.sh</code> and open the dashboard",
     "Everything is paper mode — no real money can move. Watching a live "
     "system is the fastest way to make the words below concrete."),
    ("2", "Learn the 10 glossary terms below",
     "They cover ~90% of what this dashboard and any crypto trading text "
     "assumes you know."),
    ("3", "Watch the strategies paper-trade for 1–2 weeks",
     "Read each strategy's page and the /intel &quot;why&quot; lines. Ask of "
     "every trade: who is on the other side, and why are they losing?"),
    ("4", "Start layer L1 of the architecture below (~10 hrs/week)",
     "Math first, tools second, strategies last. Skipping ahead to "
     "&quot;which indicator wins&quot; is how beginners lose money."),
    ("5", "Only after months of study + paper results, read "
     "<code>docs/live-checklist.md</code>",
     "The verified methods below earn single-digit-to-low-double-digit APR. "
     "Anyone promising more is selling you something."),
)

# Verified earning methods (evidence-ranked), condensed from
# docs/RESEARCH-EARNING.md which carries the full per-claim source links:
# (rank, name, how it works, realistic return, what kills it, evidence, repo)
_LEARN_METHODS: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (
        "1", "Funding-rate carry 资金费率套利",
        "Hold spot long + perp short. Price moves cancel out; you collect the "
        "funding payments that leveraged longs pay, every 1–8 hours.",
        "5–20% APR typical; episodic spikes to 70%+ APR in hot regimes.",
        "Funding flips negative in bear markets (needs an exit rule); the "
        "short leg can be margin-called in violent pumps (keep leverage ≤2x); "
        "venue failure (FTX) — split across exchanges.",
        "Academic full-sample ≈8%/yr low-vol (CMU, <em>The Crypto Carry "
        "Trade</em>); 2025 peer-reviewed CEX/DEX funding-arb study: up to "
        "115.9%/6mo with max loss &lt;2% in-window (ScienceDirect); only ~40% "
        "of cross-venue spreads ≥20 bps survive fees (MDPI).",
        "src/qt/strategies/carry.py · intel FundingScanner",
    ),
    (
        "2", "Crash / capitulation buying 暴跌抄底",
        "Buy only when multiple independent factor groups confirm forced "
        "selling (liquidation cascades), and macro doesn't veto. The seller "
        "isn't selling on information — a margin engine made them sell.",
        "A few high-quality entries per cycle; historically strong forward "
        "returns from capitulation lows.",
        "Catching a falling knife in a true regime break; first bounce is "
        "often short-covering — exit on mean reversion, don't &quot;hold "
        "forever&quot;.",
        "Caporale et al. 2018 (short-horizon BTC reversal); Gkillas &amp; "
        "Katsiampa 2018 (EVT tails); Oct-2025 cascade: −12% in 8h, ~$19B "
        "liquidated, 87% longs (CCN post-mortem) — see docs/strategy.md.",
        "src/qt/strategies/capitulation.py",
    ),
    (
        "3", "Wick catching 插针捕捉",
        "Rest deep limit buys (−5%/−8%/−12%, off round numbers); liquidation "
        "cascades wick through them at panic prices; take profit on the "
        "bounce. Small, steady; pays only in flash events.",
        "Small per-event gains; 10–30% intra-minute dips happen multiple "
        "times a year.",
        "Fills then keeps falling (regime break, not a wick) — small rungs, "
        "hard stop below the ladder, macro veto; no fill guarantee in gaps.",
        "Cascade mechanics (Bit.com); flash-crash order behavior (B2Prime, "
        "Kraken); grid-bot literature shows the same volatility-harvesting "
        "premium and the same trending-breakout failure mode (Bitsgap).",
        "src/qt/strategies/wick_catcher.py",
    ),
    (
        "4", "Episodic dislocations 事件性错位",
        "Rare panics leave big mispricings open for minutes-to-days: "
        "stablecoin depegs, cross-venue spreads in cascades. Machine scans "
        "24/7 and alerts; a human confirms — the edge is judgment, not speed.",
        "Rare but large: USDC Mar-2023 traded ~$0.88 while redeemable at "
        "$1.00; peg restored in ~2 days.",
        "The depeg can be real (UST went to zero) — you must verify issuer "
        "solvency; capital locked mid-crisis.",
        "CoinDesk on-chain analysis of the USDC repeg (one wallet +$16.5M); "
        "Chainalysis 2025: ≥0.5% cross-venue gaps still occur thousands of "
        "times daily, capturable only in the fat tail.",
        "src/qt/intel/scanners.py (DepegScanner, SpreadScanner)",
    ),
    (
        "✗", "Speed arbitrage — do NOT attempt 别做高频套利",
        "Cross-exchange latency arb, triangular loops, CEX–DEX MEV. Windows "
        "on majors last under ~4 seconds and require colocation or "
        "block-builder relationships a personal operator doesn't have.",
        "Effectively zero at personal scale, after fees and losses to "
        "professionals.",
        "You are the prey, not the predator: 3 MEV searchers captured ~75% "
        "of $233.8M in CEX–DEX arb profits over 19 months.",
        "arXiv, <em>The Darkest of the MEV Dark Forest</em>; CoinAPI 2025 "
        "(windows &lt;4s, cross-venue variance −78% since 2020).",
        "intel scanners detect &amp; alert only — no execution built",
    ),
)

# 6-month spiral plan: (month, focus, deliverable)
_LEARN_PLAN: tuple[tuple[str, str, str], ...] = (
    ("1", "Mindset + probability/stats; Python setup",
     "Reproduce Sharpe &amp; Z-score in numpy; start a research journal"),
    ("2", "pandas + markets/instruments",
     "Ingest BTC OHLCV; rebuild one indicator from docs/indicators.md"),
    ("3", "Time series + volatility",
     "Fit GARCH; replicate the vol-ratio regime signal"),
    ("4", "Single-factor research",
     "Test one factor's IC honestly, out-of-sample"),
    ("5", "Sizing + backtesting/validation",
     "Run walk-forward + Monte Carlo; overfit-on-purpose once"),
    ("6", "Execution + review",
     "Add realistic costs; write an honest strategy post-mortem"),
)

# Curated references shown on the page (subset mirrors docs/LEARNING.md).
_LEARN_REFS: tuple[str, ...] = (
    "López de Prado, M. (2018). <em>Advances in Financial Machine Learning</em>. Wiley.",
    "Wasserman, L. (2004). <em>All of Statistics</em>. Springer.",
    "Ross, S. (2018). <em>A First Course in Probability</em> (10th ed.). Pearson.",
    "Shreve, S. (2004). <em>Stochastic Calculus for Finance I &amp; II</em>. Springer.",
    "McKinney, W. (2022). <em>Python for Data Analysis</em> (3rd ed.). O'Reilly.",
    "Hull, J. (2021). <em>Options, Futures, and Other Derivatives</em> (11th ed.). Pearson.",
    "Tsay, R. (2010). <em>Analysis of Financial Time Series</em> (3rd ed.). Wiley.",
    "Grinold, R. &amp; Kahn, R. (1999). <em>Active Portfolio Management</em> (2nd ed.). "
    "McGraw-Hill — the Fundamental Law, IR ≈ IC·√breadth.",
    "Narang, R. (2013). <em>Inside the Black Box</em> (2nd ed.). Wiley.",
    "Fama, E. &amp; French, K. (1993). &quot;Common Risk Factors in the Returns on Stocks "
    "and Bonds.&quot; <em>J. Financial Economics</em>.",
    "Harvey, C., Liu, Y. &amp; Zhu, H. (2016). &quot;…and the Cross-Section of Expected "
    "Returns.&quot; <em>Review of Financial Studies</em> — the multiple-testing bar.",
    "Markowitz, H. (1952). &quot;Portfolio Selection.&quot; <em>J. Finance</em>.",
    "Taleb, N. N. (2001/2007). <em>Fooled by Randomness</em> / <em>The Black Swan</em>.",
    "Kahneman, D. (2011). <em>Thinking, Fast and Slow</em>. FSG.",
    "Duke, A. (2018). <em>Thinking in Bets</em>. Portfolio.",
    "MIT OpenCourseWare — 18.05 <em>Probability &amp; Statistics</em>; 15.401 "
    "<em>Finance Theory I</em> (A. Lo). ocw.mit.edu",
    "MIT OpenCourseWare — 6.0001 <em>Intro to CS &amp; Programming in Python</em>. ocw.mit.edu",
    "Haugh, M. &amp; Iyengar, G. — <em>Financial Engineering &amp; Risk Management</em>, "
    "Columbia (Coursera).",
    "QuantConnect — Boot Camp &amp; Lean engine docs. quantconnect.com",
    "Hyndman, R. &amp; Athanasopoulos, G. (2021). <em>Forecasting: Principles &amp; "
    "Practice</em> (3rd ed.). Free at otexts.com/fpp3/",
    "Kelly, J. L. (1956). &quot;A New Interpretation of Information Rate.&quot; "
    "<em>Bell System Technical Journal</em> (see Thorp on fractional Kelly).",
    "Bailey, D. &amp; López de Prado, M. (2014). &quot;The Deflated Sharpe Ratio.&quot; "
    "<em>J. Portfolio Management</em>.",
    "Bailey, Borwein, López de Prado &amp; Zhu (2014). &quot;Pseudo-Mathematics and "
    "Financial Charlatanism.&quot; <em>Notices of the AMS</em>, 61(5).",
    "Chan, E. (2009/2013). <em>Quantitative Trading</em> / <em>Algorithmic Trading</em>. Wiley.",
    "Almgren, R. &amp; Chriss, N. (2000). &quot;Optimal Execution of Portfolio "
    "Transactions.&quot; <em>J. Risk</em>.",
    "Kyle, A. (1985). &quot;Continuous Auctions and Insider Trading.&quot; "
    "<em>Econometrica</em> (see also O'Hara; Harris, <em>Trading &amp; Exchanges</em>).",
)


def _render_learn_intro() -> str:
    return (
        '<div class="panel prose" style="border-left:4px solid var(--accent)">'
        "<p><strong>Quant finance is a stack of dependent layers.</strong> You "
        "can't reason about a carry trade without probability, judge a backtest "
        "without understanding overfitting, or size a position without the Kelly "
        "criterion and its failure modes. Learn bottom-up — but touch the top "
        "early: build a toy strategy in week one so the theory has something to "
        "attach to.</p>"
        "<p class=\"cite\">This is the summarized version. The full, detailed "
        "companion — with every citation and the code that demonstrates each "
        "idea — lives in <code>docs/LEARNING.md</code>.</p>"
        "<p><span class=\"tag\">Rule 1</span> Learn by building, then reading, "
        "then rebuilding — reproduce a result before you trust it. "
        "<span class=\"tag\">Rule 2</span> Keep a dated research journal; its "
        "absence is the biggest cause of backtest overfitting [1]. "
        "<span class=\"tag\">Rule 3</span> Assume you are fooling yourself — most "
        "&quot;discovered&quot; alphas fail a proper multiple-testing bar [11].</p>"
        "</div>"
    )


def _render_learn_start() -> str:
    rows = "".join(
        f"<tr><td><strong>{step}</strong></td><td>{action}</td><td>{why}</td></tr>"
        for step, action, why in _LEARN_START
    )
    return (
        "<table><thead><tr><th></th><th>Do this</th><th>Why</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _render_learn_glossary() -> str:
    rows = "".join(
        f"<tr><td><strong>{_e(term)}</strong></td><td>{_e(meaning)}</td></tr>"
        for term, meaning in _LEARN_GLOSSARY
    )
    return (
        "<table><thead><tr><th>Term</th><th>Plain meaning</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _render_learn_methods() -> str:
    cards: list[str] = []
    for rank, name, how, ret, kills, evidence, repo in _LEARN_METHODS:
        avoid = rank == "✗"
        tag_cls = ' style="background:#f8e7e7;color:var(--bad)"' if avoid else ""
        cards.append(
            '<div class="panel prose">'
            f'<h3><span class="tag"{tag_cls}>{rank}</span>{name}</h3>'
            f"<p>{how}</p>"
            f"<p><strong>Realistic return:</strong> {ret}</p>"
            f"<p><strong>What kills it:</strong> {kills}</p>"
            f'<p class="cite"><strong>Evidence:</strong> {evidence}</p>'
            f'<p class="cite">In this repo: <code>{_e(repo)}</code></p>'
            "</div>"
        )
    return '<div class="cards">' + "".join(cards) + "</div>"


def _render_learn_layers() -> str:
    cards: list[str] = []
    for lid, title, why, concepts, where, how, repo in _LEARN_LAYERS:
        cards.append(
            '<div class="panel prose">'
            f'<h3><span class="tag">{lid}</span>{title}</h3>'
            f"<p>{why}</p>"
            f'<p><strong>Master:</strong> {concepts}</p>'
            f'<p><strong>Where:</strong> {where}</p>'
            f'<p><strong>How:</strong> {how}</p>'
            f'<p class="cite">In this repo: <code>{_e(repo)}</code></p>'
            "</div>"
        )
    return '<div class="cards">' + "".join(cards) + "</div>"


def _render_learn_plan() -> str:
    rows = "".join(
        f"<tr><td><strong>Month {m}</strong></td><td>{focus}</td>"
        f"<td>{deliverable}</td></tr>"
        for m, focus, deliverable in _LEARN_PLAN
    )
    return (
        "<table><thead><tr><th>When</th><th>Focus</th>"
        "<th>Deliverable</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _render_learn_refs() -> str:
    items = "".join(f"<li>{ref}</li>" for ref in _LEARN_REFS)
    return f'<ol class="refs">{items}</ol>'


def _render_learn_page(context: DashboardContext) -> str:
    body = f"""
    {_render_learn_intro()}
    <h2>Start here — you have little knowledge, that's fine 从零开始</h2>
    <div class="subtle" style="margin-bottom:10px">Five steps before any
      textbook. The system runs in paper mode, so nothing here risks money.</div>
    {_render_learn_start()}
    <h2>The 10 words you must know first 必学词汇</h2>
    {_render_learn_glossary()}
    <h2>Verified methods that actually earn — with evidence 经过验证的赚钱方法</h2>
    <div class="subtle" style="margin-bottom:10px">Ranked by evidence strength,
      from <code>docs/RESEARCH-EARNING.md</code> (which carries every source
      link). The honest meta-lesson: at personal scale the durable edges are
      <strong>patience edges, not speed edges</strong> — carry monetizes other
      people's leverage demand; crash/wick buying monetizes their forced
      liquidations; depeg buying monetizes their panic.</div>
    {_render_learn_methods()}
    <h2>The knowledge architecture — 9 layers</h2>
    <div class="subtle" style="margin-bottom:10px">Bottom-up dependencies:
      L0 mindset wraps everything; L1 math → L9 behavior. Spiral through them,
      going deeper each pass.</div>
    {_render_learn_layers()}
    <h2>A concrete 6-month study plan</h2>
    <div class="subtle" style="margin-bottom:10px">Part-time (~10 hrs/week).
      Then repeat the spiral at greater depth — competence is measured in
      reproduced results, not books read.</div>
    {_render_learn_plan()}
    <h2>How to learn (the method)</h2>
    <div class="panel prose">
      <ul>
        <li><strong>Deliberate practice:</strong> work at the edge of your
          ability with fast feedback — do problems, don't just read.</li>
        <li><strong>Falsify, don't confirm:</strong> try to kill every idea
          before you trust it.</li>
        <li><strong>Process over outcome:</strong> a good decision can lose and
          a bad one can win — judge the process (Duke [15]).</li>
        <li><strong>Sizing beats prediction:</strong> survival first, edge
          second (Kelly [21], fractional).</li>
        <li><strong>Use this repo as a lab:</strong> every layer above points to
          the code that makes it concrete — read it, break it, rebuild it.</li>
      </ul>
      <p class="cite">A caution on communities: forums contain gems and
        confident nonsense in equal measure. Trust papers with reproducible
        methods and your own out-of-sample tests over anyone's screenshot of an
        equity curve.</p>
    </div>
    <h2>References &amp; where to learn</h2>
    <div class="panel">{_render_learn_refs()}</div>
    <div class="subtle" style="margin-top:12px">
      See also, in this repo:
      <a href="/intel">Intelligence</a> (live worked examples) ·
      <code>docs/strategy.md</code> ·
      <code>docs/indicators.md</code> ·
      <code>docs/RESEARCH-EARNING.md</code> ·
      <code>docs/ROADMAP.md</code>.
    </div>
    """
    return _page(
        title="QT — Learn Quant",
        heading="Learn Quant",
        subtitle="A sound, citation-backed path from zero to systematic trading.",
        body=body,
        current="/learn",
        aside='detailed companion: <code>docs/LEARNING.md</code>',
        max_width=1080,
        refresh=0,
    )


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
    return _page(
        title="QT — Portfolio P&amp;L",
        heading="Portfolio P&amp;L",
        subtitle="Paper-trading account book per strategy. Are we making money?",
        body=body,
        current="/portfolio",
        aside="refreshes 60s",
        max_width=1080,
    )


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
