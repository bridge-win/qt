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
from qt.research.repository import ResearchRepository


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


def test_backtest_job_rejects_rules_that_silently_replace_template(
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
    assert status == 400
    payload = json.loads(body)
    assert payload["ok"] is False
    assert "custom_rule_recipe" in payload["errors"][0]


def test_backtest_builder_page_has_beginner_controls(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, _, body = _get(port, "/backtest/build")
    assert status == 200
    assert "BTC Research Flight Plan" in body
    assert "1 · Choose your goal" in body
    assert "2 · Select verified data" in body
    assert "3 · Build the strategy" in body
    assert "4 · Set assumptions" in body
    assert "5 · Review and run" in body
    assert "10-year standard is not installed" in body
    assert "Sync 10-year Bitstamp data" in body
    assert "/api/v2/backtests/datasets/bitstamp-btcusd-1d-10y/sync" in body
    assert 'id="template-parameters"' in body
    assert "readTemplateParameters" in body


def test_v2_catalog_limits_beginner_recommendations(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard
    status, _, body = _get(port, "/api/v2/backtests/catalog")
    assert status == 200
    strategies = json.loads(body)["strategies"]
    beginner_ids = {
        item["id"] for item in strategies if item["beginner_friendly"] is True
    }
    assert beginner_ids == {
        "buy_and_hold",
        "fixed_dca",
        "smart_dca",
        "sma_crossover",
        "rsi_mean_reversion",
        "bollinger_mean_reversion",
        "donchian_breakout",
    }


def test_v2_catalog_datasets_health_and_job_contracts(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, _ = served_backtest_dashboard

    status, _, catalog_body = _get(port, "/api/v2/backtests/catalog")
    assert status == 200
    catalog = json.loads(catalog_body)
    assert catalog["api_version"] == "2"
    assert catalog["modes"] == ["template", "custom_rules", "ensemble"]

    status, _, datasets_body = _get(port, "/api/v2/backtests/datasets")
    assert status == 200
    datasets = json.loads(datasets_body)["datasets"]
    assert any(item["dataset_id"] == "okx-btcusdt-1h" for item in datasets)
    bitstamp = next(
        item
        for item in datasets
        if item["dataset_id"] == "bitstamp-btcusd-1d-10y"
    )
    assert bitstamp["status"] == "missing"
    assert bitstamp["standard_ready"] is False

    status, _, health_body = _get(port, "/api/v2/backtests/health")
    assert status == 200
    health = json.loads(health_body)
    assert health["status"] in {"healthy", "degraded"}
    assert health["worker"]["queue_limit"] == 5

    status, _, job_body = _post_json(
        port,
        "/api/v2/backtests/jobs",
        {
            "dataset_id": "okx-btcusdt-1h",
            "mode": "template",
            "template": {
                "strategy_id": "sma_crossover",
                "parameters": {"fast_window": 10, "slow_window": 30},
            },
            "validation_profile": "quick",
            "assumptions": {
                "initial_cash": 10_000,
                "fee_bps": 10,
                "slippage_bps": 5,
            },
            "seed": 7,
        },
    )
    assert status == 202
    job = json.loads(job_body)["job"]
    assert job["status"] == "queued"
    assert job["spec"]["mode"] == "template"

    status, _, cancel_body = _post_json(
        port,
        f"/api/v2/backtests/jobs/{job['job_id']}/cancel",
        {},
    )
    assert status == 200
    assert json.loads(cancel_body)["job"]["status"] == "cancelled"


def test_v2_run_series_and_artifact_allow_list(
    served_backtest_dashboard: tuple[int, Path],
) -> None:
    port, backtests = served_backtest_dashboard
    repository = ResearchRepository(backtests / "research.sqlite3")
    job = repository.enqueue({"kind": "test"})
    repository.claim_next("test-worker")
    run_id = "a" * 32
    run_dir = backtests / run_id
    run_dir.mkdir()
    (run_dir / "equity.csv").write_text(
        "timestamp,strategy,buy_and_hold,fixed_dca\n"
        "2022-01-01T00:00:00+00:00,10000,10000,10000\n",
        encoding="utf-8",
    )
    (run_dir / "trades.csv").write_text(
        "ts,side,qty,price,fee,pnl\n",
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "data_manifest.json").write_text("{}\n", encoding="utf-8")
    repository.complete(
        str(job["job_id"]),
        {
            "run_id": run_id,
            "configuration": {"ohlcv_key": "okx_BTCUSDT_1h"},
            "artifacts": [
                "data_manifest.json",
                "equity.csv",
                "summary.json",
                "trades.csv",
            ],
        },
    )

    status, _, series_body = _get(
        port, f"/api/v2/backtests/runs/{run_id}/series"
    )
    assert status == 200
    assert json.loads(series_body)["equity"]

    status, content_type, artifact_body = _get(
        port,
        f"/api/v2/backtests/runs/{run_id}/artifacts/equity.csv",
    )
    assert status == 200
    assert content_type == "text/csv; charset=utf-8"
    assert "strategy,buy_and_hold,fixed_dca" in artifact_body

    status, _, _ = _get(
        port,
        f"/api/v2/backtests/runs/{run_id}/artifacts/research.sqlite3",
    )
    assert status == 404

    status, _, result_page = _get(port, f"/backtest/runs/{run_id}")
    assert status == 200
    assert "Parameter sensitivity heatmap" in result_page
    assert "Monte Carlo percentile paths" in result_page
    assert "Monthly returns and trade ledger" in result_page


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
