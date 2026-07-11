"""Tests for the /learn page, shared page shell, and top navigation."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path
from socketserver import TCPServer

import pytest

from qt.dashboard.server import (
    _LEARN_LAYERS,
    _LEARN_REFS,
    DashboardContext,
    _make_handler,
    _render_learn_page,
    _top_nav,
)


@pytest.fixture()
def context(tmp_path: Path) -> DashboardContext:
    (tmp_path / "parquet").mkdir()
    (tmp_path / "backtests").mkdir()
    return DashboardContext(
        parquet_dir=tmp_path / "parquet",
        backtests_dir=tmp_path / "backtests",
        monitor_state_path=tmp_path / "monitor.json",
        strategies_state_dir=tmp_path / "strategies",
        runtime_dir=tmp_path,
    )


@pytest.fixture()
def served_dashboard(context: DashboardContext) -> Iterator[int]:
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


def test_learn_page_served(served_dashboard: int) -> None:
    status, body = _get(served_dashboard, "/learn")
    assert status == 200
    text = body.decode()
    assert "Learn Quant" in text
    # Knowledge architecture layers are present.
    assert "The knowledge architecture" in text
    assert "9 layers" in text
    # The 6-month plan and method section render.
    assert "6-month study plan" in text
    assert "How to learn" in text


def test_learn_page_has_citations(context: DashboardContext) -> None:
    html = _render_learn_page(context)
    # A representative spread of the reference list is present.
    for token in ("López de Prado", "Kelly", "Wasserman", "Hull", "Kahneman"):
        assert token in html, token
    # Every reference in the data table makes it into the HTML.
    assert html.count("<li>") >= len(_LEARN_REFS)


def test_learn_page_maps_to_repo_code(context: DashboardContext) -> None:
    html = _render_learn_page(context)
    # Each layer cross-references real code/docs in this repo.
    assert "src/qt/risk/sizing.py" in html
    assert "src/qt/backtest/walkforward.py" in html
    assert "docs/LEARNING.md" in html
    # All nine architecture layers are rendered.
    for lid, *_ in _LEARN_LAYERS:
        assert f">{lid}</span>" in html


def test_top_nav_marks_current() -> None:
    nav = _top_nav("/learn")
    assert 'href="/learn" class="here"' in nav
    # Non-current links are plain.
    assert 'href="/">Monitor</a>' in nav
    # Every primary destination is linked.
    for href in ("/", "/portfolio", "/intel", "/learn"):
        assert f'href="{href}"' in nav


def test_every_full_page_shares_nav(served_dashboard: int) -> None:
    for path in ("/", "/portfolio", "/intel", "/learn"):
        status, body = _get(served_dashboard, path)
        assert status == 200, path
        text = body.decode()
        assert 'nav class="top"' in text, path
        assert 'href="/learn"' in text, path
