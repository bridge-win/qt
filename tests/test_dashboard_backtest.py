from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from html import unescape
from http.client import HTTPConnection
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import urlencode

import pytest
from btc_backtest.strategies.registry import BUILTIN_STRATEGY_IDS

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


def _post_json(port: int, path: str, payload: dict[str, object]) -> tuple[int, str, str]:
    body = json.dumps(payload)
    connection = HTTPConnection("127.0.0.1", port, timeout=20)
    connection.request(
        "POST",
        path,
        body=body,
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    text = response.read().decode("utf-8")
    content_type = response.getheader("Content-Type", "")
    connection.close()
    return response.status, content_type, text


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
    assert "Auto compare strategies" in body
    assert "Let QT test DCA, trend, carry, and wick" in body


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


def test_backtest_catalog_api_exposes_full_guided_catalog(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, content_type, body = _get(port, "/api/v1/backtest/catalog")
    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    payload = json.loads(body)
    strategy_ids = [item["id"] for item in payload["strategies"]]
    assert strategy_ids[: len(BUILTIN_STRATEGY_IDS)] == list(BUILTIN_STRATEGY_IDS)
    assert payload["defaults"]["strategy_id"] == "sma_crossover"
    first_strategy = payload["strategies"][0]
    assert first_strategy["explain_like_beginner"]
    assert first_strategy["parameter_guide"]
    assert first_strategy["risk_notes"]
    assert "trend" in payload["groups"]
    assert payload["data"]["standard"]["provider"] == "bitstamp"
    assert payload["data"]["standard"]["years"] == 10


def test_backtest_recipe_validation_rejects_unavailable_ohlcv(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, content_type, body = _post_json(
        port,
        "/api/v1/backtest/recipes/validate",
        {
            "strategy_id": "sma_crossover",
            "ohlcv_key": "binance_BTCUSDT_1h",
            "initial_cash": 10_000,
            "rules": {
                "entry": {"operator": "ALL", "conditions": [{"indicator": "close_above_sma", "window": 200}]},
                "exit": {"operator": "ANY", "conditions": [{"indicator": "close_below_sma", "window": 200}]},
            },
        },
    )
    assert status == 400
    assert content_type == "application/json; charset=utf-8"
    payload = json.loads(body)
    assert payload["ok"] is False
    assert "has no rows" in payload["errors"][0]


def test_backtest_job_api_runs_and_publishes_result_route(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, backtests = served_backtest_dashboard
    status, content_type, body = _post_json(
        port,
        "/api/v1/backtest/jobs",
        {
            "strategy_id": "sma_crossover",
            "ohlcv_key": "okx_BTCUSDT_1h",
            "initial_cash": 10_000,
            "fee_bps": 10,
            "slippage_bps": 5,
        },
    )
    assert status == 202
    assert content_type == "application/json; charset=utf-8"
    submitted = json.loads(body)
    assert submitted["job"]["status"] in {"queued", "running", "complete"}
    assert submitted["job"]["stages"][0]["name"] == "queued"

    job_id = submitted["job"]["job_id"]
    final_payload: dict[str, object] | None = None
    for _ in range(20):
        status, _, job_body = _get(port, f"/api/v1/backtest/jobs/{job_id}")
        assert status == 200
        payload = json.loads(job_body)
        if payload["job"]["status"] == "complete":
            final_payload = payload
            break
        time.sleep(0.2)
    assert final_payload is not None
    job = final_payload["job"]
    assert isinstance(job, dict)
    assert job["progress"] == 100
    result = job["result"]
    assert isinstance(result, dict)
    assert result["strategy"] == "sma_crossover"
    assert result["run_id"]
    assert (backtests / "latest.json").exists()

    run_id = result["run_id"]
    status, _, result_page = _get(port, f"/backtest/runs/{run_id}")
    assert status == 200
    assert "Research verdict" in result_page
    assert "sma_crossover" in result_page


def test_backtest_job_applies_submitted_rule_recipe(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, _, body = _post_json(
        port,
        "/api/v1/backtest/jobs",
        {
            "strategy_id": "sma_crossover",
            "ohlcv_key": "okx_BTCUSDT_1h",
            "initial_cash": 10_000,
            "rules": {
                "entry": {
                    "operator": "ALL",
                    "conditions": [{"indicator": "close_above_sma", "window": 12}],
                },
                "exit": {
                    "operator": "ANY",
                    "conditions": [{"indicator": "close_below_sma", "window": 12}],
                },
            },
        },
    )
    assert status == 202
    job_id = json.loads(body)["job"]["job_id"]
    result: dict[str, object] | None = None
    for _ in range(20):
        status, _, job_body = _get(port, f"/api/v1/backtest/jobs/{job_id}")
        assert status == 200
        job = json.loads(job_body)["job"]
        if job["status"] == "complete":
            raw_result = job["result"]
            assert isinstance(raw_result, dict)
            result = raw_result
            break
        time.sleep(0.2)
    assert result is not None
    recipe = result["recipe"]
    assert isinstance(recipe, dict)
    assert recipe["entry_operator"] == "ALL"
    assert recipe["exit_operator"] == "ANY"
    assert recipe["conditions"] == ["close_above_sma", "close_below_sma"]


def test_backtest_builder_page_has_beginner_controls(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, _, body = _get(port, "/backtest/build")
    assert status == 200
    assert "Build a rule recipe" in body
    assert "ALL entry conditions" in body
    assert "ANY exit condition" in body
    assert "10-year Bitstamp standard" in body
    assert "Run research job" in body


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


def test_backtest_post_auto_compares_safe_strategies(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, body = _post(
        port,
        "/backtest/run",
        {
            "mode": "compare",
            "strategy": "dca",
            "ohlcv_key": "okx_BTCUSDT_1h",
            "initial_cash": "100000",
        },
    )
    assert status == 200
    assert "Auto Comparison Complete" in body
    assert "Recommended first paper candidate" in body
    assert "Compare strategies on the same data" in body
    assert "data-strategy-rank=\"dca\"" in body
    assert "data-strategy-rank=\"trend\"" in body
    assert "data-strategy-rank=\"carry\"" in body
    assert "data-strategy-rank=\"wick\"" in body
