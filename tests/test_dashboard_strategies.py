"""Dashboard sub-route + JSON tests for per-strategy state files."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from socketserver import TCPServer

import pytest

from qt.dashboard.server import DashboardContext, _make_handler, serve_dashboard


def _write_state(dir_: Path, name: str, **overrides: object) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "mode": "paper",
        "status": "healthy",
        "started_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:10:00+00:00",
        "cycle": 7,
        "consecutive_failures": 0,
        "last_error": None,
        "next_run_at": "2024-01-01T01:00:00+00:00",
        "details": {
            "description": f"test strategy {name}",
            "params": {"foo": 1},
            "last_evaluation": {
                "ts": "2024-01-01T00:10:00+00:00",
                "opportunity": None,
                "metrics": {"score": 0.42},
                "notes": "",
            },
        },
    }
    payload.update(overrides)
    p = dir_ / f"{name}.json"
    p.write_text(json.dumps(payload))
    return p


@pytest.fixture()
def served_dashboard(tmp_path: Path):
    parquet = tmp_path / "parquet"
    parquet.mkdir()
    backtests = tmp_path / "backtests"
    backtests.mkdir()
    monitor_state = tmp_path / "monitor.json"
    strategies_dir = tmp_path / "strategies"
    _write_state(strategies_dir, "dca")
    _write_state(
        strategies_dir, "carry",
        details={
            "description": "carry strat",
            "params": {"enter_apr": 0.15},
            "last_evaluation": {
                "ts": "2024-01-01T00:10:00+00:00",
                "opportunity": {
                    "ts": "2024-01-01T00:10:00+00:00",
                    "action": "open",
                    "confidence": 0.8,
                    "reason": "funding fat",
                    "details": {"ann": 0.20},
                },
                "metrics": {"ann_funding": 0.20},
                "notes": "",
            },
            "last_opportunity": {
                "ts": "2024-01-01T00:10:00+00:00",
                "action": "open",
                "confidence": 0.8,
                "reason": "funding fat",
                "details": {"ann": 0.20},
            },
        },
    )
    # Seed a portfolio ledger for dca so /portfolio has data
    from datetime import datetime, timezone

    from qt.core.types import OrderSide, Trade
    from qt.portfolio import PortfolioLedger

    ledger = PortfolioLedger("dca", tmp_path)
    ledger.set_initial_cash(100_000.0)
    ledger.record_trade(
        Trade(
            ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
            symbol="BTC/USDT", side=OrderSide.BUY, qty=0.1, price=50_000.0, fee=5.0,
        ),
        {"BTC/USDT": 50_000.0},
        100_000.0,
    )

    context = DashboardContext(
        parquet_dir=parquet, backtests_dir=backtests,
        monitor_state_path=monitor_state, strategies_state_dir=strategies_dir,
        runtime_dir=tmp_path,
    )
    handler = _make_handler(context)
    server = TCPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


def _get(port: int, path: str) -> tuple[int, bytes]:
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def test_home_lists_strategies(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/")
    assert status == 200
    text = body.decode()
    assert "Strategies" in text
    assert ">dca<" in text
    assert ">carry<" in text


def test_api_strategies_returns_both(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/api/strategies")
    assert status == 200
    data = json.loads(body)
    names = sorted(s["name"] for s in data["strategies"])
    assert names == ["carry", "dca"]


def test_api_strategy_detail(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/api/strategy/carry")
    assert status == 200
    data = json.loads(body)
    assert data["strategy"]["name"] == "carry"
    assert data["strategy"]["details"]["last_opportunity"]["action"] == "open"


def test_strategy_html_page(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/strategy/dca")
    assert status == 200
    assert b"dca" in body
    assert b"Latest Metrics" in body


def test_unknown_strategy_404(served_dashboard: int) -> None:
    status, _ = _get(served_dashboard, "/strategy/does-not-exist")
    assert status == 404


def test_portfolio_page_renders(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/portfolio")
    assert status == 200
    text = body.decode()
    assert "Portfolio P&amp;L" in text
    assert "dca" in text


def test_api_portfolios_returns_dca(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/api/portfolios")
    assert status == 200
    data = json.loads(body)
    names = [p["name"] for p in data["portfolios"]]
    assert "dca" in names
    dca = next(p for p in data["portfolios"] if p["name"] == "dca")
    assert dca["num_trades"] == 1
    assert dca["positions"]["BTC/USDT"] == 0.1


def test_api_portfolio_detail(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/api/portfolio/dca")
    assert status == 200
    data = json.loads(body)
    assert data["portfolio"]["name"] == "dca"
    assert data["portfolio"]["total_fees"] == 5.0


def test_home_shows_pnl_section(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/")
    assert status == 200
    assert b"Portfolio P&amp;L" in body


def test_home_plain_language_banner(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/")
    assert status == 200
    text = body.decode()
    # Plain-language bilingual banner is present
    assert "This week" in text
    assert "本周" in text
    assert "strategy(ies) running" in text


def test_intel_page_and_api(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/intel")
    assert status == 200
    assert b"Intelligence Discovery" in body
    status, body = _get(served_dashboard, "/api/intel")
    assert status == 200
    data = json.loads(body)
    assert "findings" in data  # empty list is fine — no scan ran


def test_serve_dashboard_signature_accepts_strategies_dir(tmp_path: Path) -> None:
    """Just verify the signature accepts the new kwarg (we don't actually serve)."""
    import inspect
    sig = inspect.signature(serve_dashboard)
    assert "strategies_state_dir" in sig.parameters
