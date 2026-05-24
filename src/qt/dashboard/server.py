"""Dependency-free local dashboard server."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypeAlias
from urllib.parse import urlparse

from qt.backtest.artifacts import latest_backtest_summary
from qt.data.catalog import data_source_statuses
from qt.data.store import ParquetStore
from qt.monitoring.state import MonitorStateStore

JsonDict: TypeAlias = dict[str, object]


@dataclass(frozen=True)
class DashboardContext:
    parquet_dir: Path
    backtests_dir: Path
    monitor_state_path: Path


def serve_dashboard(
    *,
    host: str,
    port: int,
    parquet_dir: str | Path,
    backtests_dir: str | Path,
    monitor_state_path: str | Path,
) -> None:
    context = DashboardContext(
        parquet_dir=Path(parquet_dir),
        backtests_dir=Path(backtests_dir),
        monitor_state_path=Path(monitor_state_path),
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


def _render_home(context: DashboardContext) -> str:
    sources = _sources(context)
    backtest = _latest_backtest(context)
    monitor = _monitor(context)
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
        <div class="subtle">Data coverage, live heartbeat, and latest backtest artifacts.</div>
      </div>
      <div class="subtle mono">refreshes every 60s</div>
    </header>
    {_render_monitor_cards(monitor)}
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
