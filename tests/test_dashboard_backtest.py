from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from html import unescape
from http.client import HTTPConnection
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import urlencode

import pytest

from qt.backtest.strategy_backtest import synthetic_btc_ohlcv
from qt.dashboard.server import DashboardContext, _make_handler
from qt.data.store import ParquetStore


@pytest.fixture()
def served_backtest_dashboard(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    parquet = tmp_path / "parquet"
    backtests = tmp_path / "backtests"
    strategies_dir = tmp_path / "strategies"
    parquet.mkdir()
    backtests.mkdir()
    strategies_dir.mkdir()
    store = ParquetStore(parquet)
    store.write("ohlcv", "okx_BTCUSDT_1h", synthetic_btc_ohlcv(days=45))
    store.write("ohlcv", "binance_BTCUSDT_1h", synthetic_btc_ohlcv(days=1).head(0))
    context = DashboardContext(
        parquet_dir=parquet,
        backtests_dir=backtests,
        monitor_state_path=tmp_path / "monitor.json",
        strategies_state_dir=strategies_dir,
        runtime_dir=tmp_path,
    )
    server = TCPServer(("127.0.0.1", 0), _make_handler(context))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1]), backtests
    finally:
        server.shutdown()
        server.server_close()


def _get(port: int, path: str) -> tuple[int, str, str]:
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    content_type = response.getheader("Content-Type", "")
    connection.close()
    return response.status, content_type, body


def _post(port: int, path: str, payload: dict[str, str]) -> tuple[int, str]:
    body = urlencode(payload)
    connection = HTTPConnection("127.0.0.1", port, timeout=20)
    connection.request(
        "POST",
        path,
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response = connection.getresponse()
    text = response.read().decode("utf-8")
    connection.close()
    return response.status, text


def test_backtest_route_lists_strategies_and_local_ohlcv(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, content_type, body = _get(port, "/backtest")
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert "Backtest" in body
    assert 'value="composite"' in body
    assert 'value="dca"' in body
    assert "okx_BTCUSDT_1h" in body


def test_backtest_route_explains_operator_workflow(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, _, body = _get(port, "/backtest")
    assert status == 200
    assert "Step 1 · Choose what to test" in body
    assert "Step 2 · Run the replay" in body
    assert "Step 3 · Read the result" in body
    assert "No exchange key is used and no real order is sent" in body
    assert "Backtest flight recorder" in body
    assert "id=\"run-status\"" in body
    assert "TradingView Lightweight Charts" in body


def test_backtest_options_api_returns_safe_choices(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, content_type, body = _get(port, "/api/backtests/options")
    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    payload = json.loads(body)
    assert payload["defaults"]["ohlcv_key"] == "okx_BTCUSDT_1h"
    assert payload["strategies"] == ["composite", "dca", "trend", "carry", "wick"]
    assert [item["key"] for item in payload["ohlcv_keys"]] == ["okx_BTCUSDT_1h"]


def test_backtest_post_rejects_unknown_strategy(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, body = _post(
        port,
        "/backtest/run",
        {
            "strategy": "shell",
            "ohlcv_key": "okx_BTCUSDT_1h",
            "initial_cash": "100000",
        },
    )
    assert status == 400
    assert "unknown strategy" in body


def test_backtest_post_rejects_empty_ohlcv_file(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, body = _post(
        port,
        "/backtest/run",
        {
            "strategy": "dca",
            "ohlcv_key": "binance_BTCUSDT_1h",
            "initial_cash": "100000",
        },
    )
    assert status == 400
    assert "unavailable OHLCV key" in body
    assert "has no rows" in body


def test_backtest_post_runs_gallery_strategy_and_publishes_latest(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, backtests = served_backtest_dashboard
    status, body = _post(
        port,
        "/backtest/run",
        {
            "strategy": "dca",
            "ohlcv_key": "okx_BTCUSDT_1h",
            "initial_cash": "100000",
        },
    )
    assert status == 200
    assert "Run Complete" in body
    latest = json.loads((backtests / "latest.json").read_text())
    assert latest["strategy"] == "dca"
    assert latest["ohlcv_key"] == "okx_BTCUSDT_1h"
    assert latest["counts"]["equity_points"] > 0


def test_backtest_post_returns_guided_result_and_chart_payload(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, body = _post(
        port,
        "/backtest/run",
        {
            "strategy": "dca",
            "ohlcv_key": "okx_BTCUSDT_1h",
            "initial_cash": "100000",
        },
    )
    assert status == 200
    assert "Run Complete" in body
    assert "Prediction result" in body
    assert "Risk readout" in body
    assert "Buy/sell markers" in body
    assert "What to learn before automation" in body
    match = re.search(
        r'<script type="application/json" id="backtest-chart-data">(.*?)</script>',
        body,
        re.S,
    )
    assert match is not None
    payload = json.loads(unescape(match.group(1)))
    assert payload["ohlcv_key"] == "okx_BTCUSDT_1h"
    assert len(payload["candles"]) > 10
    assert len(payload["equity"]) > 10
    assert isinstance(payload["markers"], list)
