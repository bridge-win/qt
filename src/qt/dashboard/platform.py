"""Operator-facing platform guide and paper trading demo pages."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from typing import TypeAlias

from qt.backtest.strategy_backtest import synthetic_btc_ohlcv
from qt.core.config import RiskConfig
from qt.core.types import OrderSide, OrderType, Position, Signal, SignalKind
from qt.execution.base import Order
from qt.execution.paper import PaperBroker
from qt.risk.engine import RiskEngine

JsonDict: TypeAlias = dict[str, object]


@dataclass(frozen=True)
class PlatformCapability:
    name: str
    route: str
    summary: str
    production_note: str


PLATFORM_CAPABILITIES: tuple[PlatformCapability, ...] = (
    PlatformCapability(
        name="Monitor",
        route="/",
        summary="Service heartbeat, data freshness, latest backtest, strategy status, and runtime errors.",
        production_note="Use this first after every deploy or restart. The service must be healthy before trading decisions matter.",
    ),
    PlatformCapability(
        name="Learning Center",
        route="/learn",
        summary="A cited study path for finance, statistics, risk, backtesting, and QT-specific exercises.",
        production_note="Keeps the operator from treating strategy output as a black box.",
    ),
    PlatformCapability(
        name="Trading Demo",
        route="/demo",
        summary="A reproducible paper-only trade walkthrough using synthetic BTC data.",
        production_note="Safe in production because it never sends a live broker order.",
    ),
    PlatformCapability(
        name="Intelligence",
        route="/intel",
        summary="Ranked funding, spread, basis, depeg, and wick candidates with edge and capacity context.",
        production_note="Use as an opportunity queue, then verify risk gates before acting.",
    ),
    PlatformCapability(
        name="Portfolio P&L",
        route="/portfolio",
        summary="Per-strategy paper ledger, equity, cash, fees, drawdown, and recent trades.",
        production_note="A strategy should prove clean paper behavior here before live trading is considered.",
    ),
    PlatformCapability(
        name="Strategy Detail",
        route="/",
        summary="One strategy's heartbeat, last opportunity, metrics, configured parameters, and P&L link.",
        production_note="Open a concrete strategy from the monitor table when strategy state files exist.",
    ),
)


def build_trading_demo() -> JsonDict:
    """Build a deterministic paper-trade demonstration using real domain components."""

    ohlcv = synthetic_btc_ohlcv(days=45, seed=29, start_price=50_000.0)
    latest = ohlcv.iloc[-1]
    mark_price = float(latest["close"])
    atr_value = float((ohlcv["high"] - ohlcv["low"]).tail(24).mean())
    returns = ohlcv["close"].pct_change().dropna()
    realized_vol_annual = float(returns.tail(24 * 14).std() * sqrt(365 * 24))
    if realized_vol_annual <= 0:
        realized_vol_annual = 0.45

    ts = ohlcv.index[-1].to_pydatetime()
    signal = Signal(
        ts=ts,
        kind=SignalKind.ENTRY_LONG,
        score=0.72,
        reasons=(
            "synthetic pullback recovered above the prior hourly close",
            "realized volatility is inside the paper-demo risk budget",
            "paper mode is enabled; no live broker is used",
        ),
        factors={
            "pullback_score": 0.81,
            "volatility_score": 0.64,
            "liquidity_score": 0.78,
        },
        target_quote_alloc=0.10,
    )
    risk = RiskEngine(RiskConfig(max_position_pct=0.12))
    broker = PaperBroker(initial_cash=100_000.0)
    position = Position(symbol="BTC/USDT")
    decision = risk.evaluate_entry(
        signal=signal,
        equity=broker.equity({"BTC/USDT": mark_price}),
        mark_price=mark_price,
        atr_value=atr_value,
        realized_vol_annual=realized_vol_annual,
        position=position,
    )

    paper_trade: JsonDict
    if decision.action == "open" and decision.size_quote > 0:
        qty = decision.size_quote / mark_price
        trade = broker.submit(
            Order(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                qty=qty,
                note="website trading demo; paper only",
            ),
            mark_price,
        )
        paper_trade = {
            "ts": trade.ts.isoformat(),
            "symbol": trade.symbol,
            "side": trade.side.value,
            "qty": trade.qty,
            "price": trade.price,
            "fee": trade.fee,
            "venue": trade.venue,
            "notional": trade.notional,
            "note": trade.note,
        }
    else:
        paper_trade = {
            "ts": ts.isoformat(),
            "symbol": "BTC/USDT",
            "side": "none",
            "qty": 0.0,
            "price": mark_price,
            "fee": 0.0,
            "venue": "paper",
            "notional": 0.0,
            "note": f"risk decision blocked demo trade: {decision.reason}",
        }

    return {
        "mode": "paper",
        "synthetic_data": True,
        "live_order_sent": False,
        "market": {
            "symbol": "BTC/USDT",
            "source": "deterministic synthetic BTC OHLCV",
            "bars": len(ohlcv),
            "latest_ts": ts.isoformat(),
            "mark_price": mark_price,
            "atr_24h": atr_value,
            "realized_vol_annual": realized_vol_annual,
        },
        "signal": {
            "ts": signal.ts.isoformat(),
            "kind": signal.kind.value,
            "score": signal.score,
            "target_quote_alloc": signal.target_quote_alloc,
            "reasons": list(signal.reasons),
            "factors": signal.factors,
        },
        "risk_decision": {
            "action": decision.action,
            "size_quote": decision.size_quote,
            "stop_price": decision.stop_price,
            "take_profit_price": decision.take_profit_price,
            "time_stop_ts": _iso_or_none(decision.time_stop_ts),
            "reason": decision.reason,
        },
        "paper_trade": paper_trade,
        "ledger_after": {
            "cash": broker.cash(),
            "position_qty": broker.position_qty("BTC/USDT"),
            "equity": broker.equity({"BTC/USDT": mark_price}),
            "open_exposure": broker.position_qty("BTC/USDT") * mark_price,
        },
        "safety": {
            "live_enabled": False,
            "broker": "PaperBroker",
            "guardrail": "No live order is created or submitted by this website demo.",
            "promotion_rule": "Use docs/live-checklist.md before any real-money configuration.",
        },
    }


def render_platform_page() -> str:
    capability_rows = "".join(
        "<tr>"
        f'<td><a href="{_e(cap.route)}"><strong>{_e(cap.name)}</strong></a></td>'
        f"<td>{_e(cap.summary)}</td>"
        f"<td>{_e(cap.production_note)}</td>"
        "</tr>"
        for cap in PLATFORM_CAPABILITIES
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QT - Platform</title>
  {_style()}
</head>
<body>
  <main>
    {_top_nav("Platform")}
    <section class="hero">
      <div>
        <p class="eyebrow">Operator console</p>
        <h1>QT platform</h1>
        <p class="lead">A paper-first quantitative trading workspace for monitoring data, testing ideas, reviewing opportunities, and proving strategy behavior before capital is at risk.</p>
        <div class="actions"><a class="button" href="/demo">Run trading demo</a><a href="/">Open monitor</a></div>
      </div>
      <div class="terminal" aria-label="Platform status">
        <div>mode: paper</div>
        <div>service: qt.service</div>
        <div>dashboard: :8765</div>
        <div>live orders: disabled by default</div>
      </div>
    </section>

    <section>
      <h2>How to use QT</h2>
      <ol class="steps">
        <li><strong>Learn the system.</strong> Start with <a href="/learn">/learn</a> so signals, backtests, and risk gates are understandable.</li>
        <li><strong>Check health.</strong> Use <a href="/">Monitor</a> to verify heartbeat, data freshness, latest backtest, and runtime errors.</li>
        <li><strong>Inspect opportunities.</strong> Use <a href="/intel">Intelligence</a> and strategy detail pages before trusting a trade idea.</li>
        <li><strong>Watch paper P&amp;L.</strong> Use <a href="/portfolio">Portfolio P&amp;L</a> to confirm fills, fees, exposure, and drawdown.</li>
        <li><strong>Promote slowly.</strong> Stay in paper mode until the live checklist, risk limits, API-key restrictions, and reconciliation steps are complete.</li>
      </ol>
    </section>

    <section>
      <h2>Platform capabilities</h2>
      <table><thead><tr><th>Surface</th><th>What it does</th><th>Production use</th></tr></thead><tbody>{capability_rows}</tbody></table>
    </section>

    <section>
      <h2>Production readiness</h2>
      <div class="readiness">
        <div><strong>Paper mode first</strong><span>Website demo and current service paths are safe by default: they use paper execution and visible ledgers.</span></div>
        <div><strong>Risk gates visible</strong><span>Position sizing, stops, take-profit, time stop, drawdown switch, and cooldown are explicit before a fill.</span></div>
        <div><strong>Deployment isolated</strong><span>QT runs from <span class="mono">/opt/qt</span> on port <span class="mono">8765</span>; Follow remains separate.</span></div>
        <div><strong>Live trading gated</strong><span>Real-money operation requires <span class="mono">docs/live-checklist.md</span>, trade-only API keys, caps, dry-run, and reconciliation.</span></div>
      </div>
    </section>
  </main>
</body>
</html>
"""


def render_demo_page() -> str:
    demo = build_trading_demo()
    market = _expect_dict(demo["market"])
    signal = _expect_dict(demo["signal"])
    risk = _expect_dict(demo["risk_decision"])
    trade = _expect_dict(demo["paper_trade"])
    ledger = _expect_dict(demo["ledger_after"])
    reasons = "".join(f"<li>{_e(str(reason))}</li>" for reason in _expect_list(signal["reasons"]))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QT - Trading Demo</title>
  {_style()}
</head>
<body>
  <main>
    {_top_nav("Trading Demo")}
    <section class="hero compact">
      <div>
        <p class="eyebrow">Paper-only walkthrough</p>
        <h1>Trading Demo</h1>
        <p class="lead">This demo runs a synthetic BTC signal through QT's risk gate and paper broker. No live order is created, signed, or sent.</p>
        <div class="actions"><a class="button" href="/api/demo">View JSON</a><a href="/platform">How the platform works</a></div>
      </div>
      <div class="terminal">
        <div>data: synthetic BTC</div>
        <div>broker: PaperBroker</div>
        <div>decision: {_e(str(risk["action"]))}</div>
        <div>live order: false</div>
      </div>
    </section>

    <section>
      <h2>Signal</h2>
      <div class="grid">
        {_metric("Score", _fmt_float(signal["score"], 2))}
        {_metric("Mark", _fmt_money(market["mark_price"]))}
        {_metric("ATR 24h", _fmt_money(market["atr_24h"]))}
        {_metric("Vol annual", _fmt_pct(market["realized_vol_annual"]))}
      </div>
      <ul class="steps small">{reasons}</ul>
    </section>

    <section>
      <h2>Risk gate</h2>
      <div class="grid">
        {_metric("Action", str(risk["action"]))}
        {_metric("Size quote", _fmt_money(risk["size_quote"]))}
        {_metric("Stop", _fmt_money(risk["stop_price"]))}
        {_metric("Take profit", _fmt_money(risk["take_profit_price"]))}
      </div>
      <p class="subtle">Reason: <span class="mono">{_e(str(risk["reason"]))}</span></p>
    </section>

    <section>
      <h2>Paper fill</h2>
      <table><tbody>
        <tr><th>Venue</th><td>{_e(str(trade["venue"]))}</td></tr>
        <tr><th>Side</th><td>{_e(str(trade["side"]))}</td></tr>
        <tr><th>Quantity</th><td class="mono">{_fmt_qty(trade["qty"])}</td></tr>
        <tr><th>Fill price</th><td class="mono">{_fmt_money(trade["price"])}</td></tr>
        <tr><th>Fee</th><td class="mono">{_fmt_money(trade["fee"])}</td></tr>
        <tr><th>Note</th><td>{_e(str(trade["note"]))}</td></tr>
      </tbody></table>
    </section>

    <section>
      <h2>Ledger result</h2>
      <div class="grid">
        {_metric("Cash", _fmt_money(ledger["cash"]))}
        {_metric("Position BTC", _fmt_qty(ledger["position_qty"]))}
        {_metric("Equity", _fmt_money(ledger["equity"]))}
        {_metric("Exposure", _fmt_money(ledger["open_exposure"]))}
      </div>
      <p class="notice">No live order was sent. Use this page to verify the control flow, not to infer profitability.</p>
    </section>
  </main>
</body>
</html>
"""


def _top_nav(current: str) -> str:
    links = (
        ("Platform", "/platform"),
        ("Monitor", "/"),
        ("Learn", "/learn"),
        ("Demo", "/demo"),
        ("Intelligence", "/intel"),
        ("Portfolio", "/portfolio"),
    )
    rendered = " ".join(
        f'<a class="{"active" if label == current else ""}" href="{href}">{label}</a>'
        for label, href in links
    )
    return f'<nav class="top"><a class="brand" href="/platform">QT</a><div>{rendered}</div></nav>'


def _style() -> str:
    return """<style>
    :root {
      color-scheme: light;
      --bg: #f6f7f3;
      --ink: #14211c;
      --muted: #66736c;
      --line: #dce2dd;
      --accent: #0b7a75;
      --panel: #ffffff;
      --soft: #eef4f1;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    body { margin: 0; background: var(--bg); color: var(--ink); }
    main { width: min(1180px, calc(100vw - 32px)); margin: 0 auto; padding: 20px 0 56px; }
    a { color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 3px; }
    .top { display: flex; justify-content: space-between; gap: 20px; align-items: center; padding: 10px 0 24px; }
    .top div { display: flex; flex-wrap: wrap; gap: 12px; justify-content: flex-end; font-size: 14px; }
    .brand { color: var(--ink); font-weight: 800; text-decoration: none; letter-spacing: .08em; }
    .active { color: var(--ink); font-weight: 700; }
    .hero { min-height: 360px; display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(280px, .75fr); gap: 28px; align-items: center; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
    .hero.compact { min-height: 300px; }
    .eyebrow { color: var(--accent); font-size: 13px; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; }
    h1 { font-size: clamp(44px, 8vw, 92px); line-height: .9; margin: 0; letter-spacing: 0; }
    h2 { font-size: 20px; margin: 34px 0 14px; letter-spacing: 0; }
    .lead { max-width: 720px; color: var(--muted); font-size: 18px; line-height: 1.55; }
    .actions { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; margin-top: 24px; }
    .button { display: inline-flex; align-items: center; min-height: 40px; padding: 0 14px; background: var(--ink); color: #fff; border-radius: 6px; text-decoration: none; font-weight: 700; }
    .terminal { background: #13231e; color: #d9f4e9; border-radius: 8px; padding: 18px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height: 1.9; box-shadow: inset 0 0 0 1px rgba(255,255,255,.08); }
    table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
    th, td { border-bottom: 1px solid var(--line); padding: 12px; text-align: left; vertical-align: top; font-size: 14px; }
    th { color: var(--muted); background: #fbfcfa; }
    tr:last-child td { border-bottom: 0; }
    .steps { margin: 0; padding-left: 22px; color: var(--muted); line-height: 1.8; }
    .steps strong { color: var(--ink); }
    .steps.small { margin-top: 14px; }
    .readiness { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .readiness div, .metric { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
    .readiness span { display: block; color: var(--muted); margin-top: 8px; line-height: 1.5; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .label, .subtle { color: var(--muted); font-size: 14px; }
    .value { margin-top: 6px; font-size: 22px; font-weight: 800; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .notice { border-left: 4px solid var(--accent); background: var(--soft); padding: 12px 14px; color: var(--ink); }
    @media (max-width: 900px) {
      .top { display: block; }
      .top div { justify-content: flex-start; margin-top: 10px; }
      .hero { grid-template-columns: 1fr; padding: 26px 0; }
      .grid, .readiness { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      table { display: block; overflow-x: auto; }
    }
    @media (max-width: 560px) {
      main { width: min(100vw - 20px, 1180px); }
      .grid, .readiness { grid-template-columns: 1fr; }
      h1 { font-size: 48px; }
      th, td { white-space: nowrap; }
    }
  </style>"""


def _metric(label: str, value: str) -> str:
    return f'<div class="metric"><div class="label">{_e(label)}</div><div class="value">{_e(value)}</div></div>'


def _fmt_money(value: object) -> str:
    number = _as_float(value)
    return "n/a" if number is None else f"{number:,.2f}"


def _fmt_float(value: object, digits: int) -> str:
    number = _as_float(value)
    return "n/a" if number is None else f"{number:.{digits}f}"


def _fmt_pct(value: object) -> str:
    number = _as_float(value)
    return "n/a" if number is None else f"{number:.2%}"


def _fmt_qty(value: object) -> str:
    number = _as_float(value)
    return "n/a" if number is None else f"{number:.6f}"


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _expect_dict(value: object) -> JsonDict:
    if isinstance(value, dict):
        return value
    raise TypeError(f"expected dict, got {type(value).__name__}")


def _expect_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    raise TypeError(f"expected list, got {type(value).__name__}")


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _e(value: str) -> str:
    return html.escape(value, quote=True)
