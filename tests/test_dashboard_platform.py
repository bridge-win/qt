from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path
from socketserver import TCPServer
from typing import cast

import pytest

from qt.dashboard.platform import (
    PLATFORM_CAPABILITIES,
    build_trading_demo,
    render_demo_page,
    render_platform_page,
)
from qt.dashboard.server import DashboardContext, _make_handler


@pytest.fixture()
def served_platform_dashboard(tmp_path: Path) -> Iterator[int]:
    context = DashboardContext(
        parquet_dir=tmp_path / "parquet",
        backtests_dir=tmp_path / "backtests",
        monitor_state_path=tmp_path / "monitor.json",
        strategies_state_dir=tmp_path / "strategies",
        runtime_dir=tmp_path,
    )
    context.parquet_dir.mkdir()
    context.backtests_dir.mkdir()
    handler = _make_handler(context)
    server = TCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _get(port: int, path: str) -> tuple[int, str, str]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    content_type = response.getheader("Content-Type", "")
    connection.close()
    return response.status, content_type, body


def test_platform_capabilities_cover_operator_surface() -> None:
    names = {capability.name for capability in PLATFORM_CAPABILITIES}
    assert {
        "Monitor",
        "Learning Center",
        "Trading Demo",
        "Intelligence",
        "Portfolio P&L",
        "Strategy Detail",
    }.issubset(names)
    assert all(capability.route.startswith("/") for capability in PLATFORM_CAPABILITIES)
    assert all(capability.production_note for capability in PLATFORM_CAPABILITIES)


def test_trading_demo_uses_paper_execution_and_risk_gate() -> None:
    demo = build_trading_demo()
    signal = cast(dict[str, object], demo["signal"])
    risk_decision = cast(dict[str, object], demo["risk_decision"])
    paper_trade = cast(dict[str, object], demo["paper_trade"])
    ledger_after = cast(dict[str, object], demo["ledger_after"])
    assert demo["mode"] == "paper"
    assert demo["synthetic_data"] is True
    assert signal["kind"] == "entry_long"
    assert risk_decision["action"] == "open"
    assert paper_trade["venue"] == "paper"
    assert paper_trade["side"] == "buy"
    equity = ledger_after["equity"]
    assert isinstance(equity, int | float)
    assert equity > 0
    assert demo["live_order_sent"] is False


def test_platform_page_explains_usage_capabilities_and_readiness() -> None:
    html = render_platform_page()
    assert "How to use QT" in html
    assert "Platform capabilities" in html
    assert "Production readiness" in html
    assert 'href="/demo"' in html
    assert 'href="/portfolio"' in html
    assert "paper mode" in html.lower()


def test_demo_page_shows_safe_trading_flow() -> None:
    html = render_demo_page()
    assert "Trading Demo" in html
    assert "synthetic BTC" in html
    assert "Risk gate" in html
    assert "Paper fill" in html
    assert "No live order" in html


def test_platform_and_demo_routes(served_platform_dashboard: int) -> None:
    status, content_type, body = _get(served_platform_dashboard, "/platform")
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert "How to use QT" in body

    status, content_type, body = _get(served_platform_dashboard, "/demo")
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert "Trading Demo" in body


def test_demo_api_returns_machine_readable_snapshot(served_platform_dashboard: int) -> None:
    status, content_type, body = _get(served_platform_dashboard, "/api/demo")
    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    data = json.loads(body)
    assert data["demo"]["mode"] == "paper"
    assert data["demo"]["live_order_sent"] is False
    assert data["demo"]["paper_trade"]["venue"] == "paper"


def test_home_navigation_links_to_platform_and_demo(served_platform_dashboard: int) -> None:
    status, _, body = _get(served_platform_dashboard, "/")
    assert status == 200
    assert 'href="/platform"' in body
    assert 'href="/demo"' in body
