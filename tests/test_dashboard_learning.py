"""Quant learning-center curriculum and HTTP rendering tests."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from socketserver import TCPServer

import pytest

from qt.dashboard.learning import MODULES, SOURCES, render_learning_page, validate_curriculum
from qt.dashboard.server import DashboardContext, _make_handler


@pytest.fixture()
def served_learning_dashboard(tmp_path: Path) -> Iterator[int]:
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


def test_curriculum_is_complete_and_cited() -> None:
    validate_curriculum(MODULES, SOURCES)
    assert [module.number for module in MODULES] == list(range(1, 9))
    assert all(len(module.concepts) >= 6 for module in MODULES)
    assert all(len(module.experience_notes) >= 2 for module in MODULES)
    assert all(module.qt_exercise for module in MODULES)
    assert all(module.source_ids for module in MODULES)


def test_curriculum_rejects_unknown_citation() -> None:
    invalid = replace(MODULES[0], source_ids=("missing",))
    with pytest.raises(ValueError, match="unknown source"):
        validate_curriculum((invalid,), SOURCES)


def test_curriculum_rejects_duplicate_source_and_insecure_url() -> None:
    with pytest.raises(ValueError, match="duplicate source"):
        validate_curriculum(MODULES, (SOURCES[0], SOURCES[0]))

    insecure = replace(SOURCES[0], url="http://example.com")
    with pytest.raises(ValueError, match="HTTPS"):
        validate_curriculum(MODULES, (insecure, *SOURCES[1:]))


def test_learning_renderer_contains_complete_learning_contract() -> None:
    page = render_learning_page()
    assert '<meta name="viewport"' in page
    assert "Learn · Derive · Apply · Challenge" in page
    assert "12-week" in page
    assert "not investment advice" in page
    assert 'rel="noopener noreferrer"' in page
    assert 'href="#source-markowitz"' in page
    assert 'id="source-markowitz"' in page
    assert "prefers-reduced-motion" in page
    assert ":focus-visible" in page
    for module in MODULES:
        assert module.title in page


def test_learning_route_returns_utf8_html(served_learning_dashboard: int) -> None:
    status, content_type, body = _get(served_learning_dashboard, "/learn")
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert "Quant Learning Center" in body
    assert 'id="module-8"' in body


def test_home_links_to_learning_center(served_learning_dashboard: int) -> None:
    status, _, body = _get(served_learning_dashboard, "/")
    assert status == 200
    assert 'href="/learn"' in body
    assert ">Learn</a>" in body
