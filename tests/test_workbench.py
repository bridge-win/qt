from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from qt.workbench.api import WorkbenchSettings, create_workbench_app
from qt.workbench.capabilities import coverage_summary, manifest
from qt.workbench.catalog import catalog_summary, legacy_catalog
from qt.workbench.catalog_signals import add_catalog_indicators, evaluate_signal
from qt.workbench.inventory import capability_matrix, capability_summary, source_audit
from qt.workbench.resources import ResourcePolicy


def test_manifest_maps_every_preserved_source_file_with_exact_provenance() -> None:
    rows = manifest()
    assert len(rows) >= 157
    assert coverage_summary()["behavior_verified"] == 0
    assert coverage_summary()["pending_behavior_verification"] >= 156
    for row in rows:
        assert len(str(row["source_commit"])) == 40
        assert Path(str(row["target_path"])).suffix in {".py", ".json"}
        assert row["migration_status"] in {
            "source_ported_pending_behavior_verification",
            "catalog_asset_verified",
        }


def test_catalog_preserves_exact_source_profile_count_and_signal_behavior() -> None:
    catalog = legacy_catalog()
    assert catalog_summary()["signal_count"] == 50
    assert catalog_summary()["profile_count"] == 100
    frame = pd.DataFrame(
        {
            "open": [100, 101, 102, 103] * 60,
            "high": [102, 103, 104, 105] * 60,
            "low": [99, 100, 101, 102] * 60,
            "close": [101, 102, 103, 104] * 60,
            "volume": [10, 12, 14, 16] * 60,
        }
    )
    enriched = add_catalog_indicators(frame)
    for signal in catalog["signals"]:
        assert evaluate_signal(str(signal["id"]), enriched, {}).dtype == bool


def test_semantic_matrix_maps_named_source_entries_without_inflating_runnable_counts() -> None:
    rows = capability_matrix()
    summary = capability_summary()
    assert summary["total_semantic_capabilities"] == len(rows)
    assert summary["requires_legacy_runtime"] >= 104
    assert summary["pending_adapter"] > 0
    audit = source_audit()
    legacy_symbols = [row for row in audit if str(row["id"]).startswith("legacy-source:")]
    assert legacy_symbols
    assert not any(row["runnable"] for row in legacy_symbols)
    assert all("@" in str(row["source"]) for row in legacy_symbols)
    assert any(row["id"] == "qt:operation:paper_broker" for row in rows)
    assert any(row["id"] == "qt:indicator-family:talib_standard" for row in rows)
    assert any(row["id"] == "qt:source:qt.indicators.price:atr" for row in audit)
    assert any(row["id"] == "btc-qt:event:btcqt_s1_wick_ladder" for row in rows)


def test_v3_existing_retry_bypasses_later_admission_and_keeps_data_fingerprint(
    tmp_path: Path,
) -> None:
    parquet_dir = tmp_path / "parquet" / "ohlcv"
    parquet_dir.mkdir(parents=True)
    index = pd.date_range("2024-01-01", periods=300, freq="h", tz="UTC")
    pd.DataFrame(
        {
            "open": range(300),
            "high": range(1, 301),
            "low": range(300),
            "close": range(1, 301),
            "volume": [1.0] * 300,
        },
        index=index,
    ).to_parquet(parquet_dir / "okx_BTCUSDT_1h.parquet")
    settings = WorkbenchSettings(
        parquet_root=tmp_path / "parquet",
        state_root=tmp_path / "state",
        resource_policy=ResourcePolicy(max_estimated_rows=1_000_000, min_available_memory_mib=0),
    )
    client = TestClient(create_workbench_app(settings))
    created_strategy = client.post(
        "/api/v3/strategies",
        json={
            "name": "immutable test strategy",
            "kind": "rules",
            "rules": {
                "entry": {
                    "kind": "signal",
                    "signal": {"indicator": "rsi", "timeframe": "1h"},
                    "comparator": "<",
                    "right": 30,
                },
                "exit": {
                    "kind": "signal",
                    "signal": {"indicator": "rsi", "timeframe": "1h"},
                    "comparator": ">",
                    "right": 55,
                },
            },
        },
    )
    assert created_strategy.status_code == 201
    version_id = created_strategy.json()["version"]["version_id"]
    payload = {
        "strategy_version_id": version_id,
        "dataset_id": "okx-btcusdt-1h",
        "precision": "bar",
        "costs": {"initial_cash": 10000, "fee_bps": 10, "slippage_bps": 5},
        "validation": "quick",
    }
    headers = {"Idempotency-Key": "retry-key"}
    first = client.post("/api/v3/experiments", headers=headers, json=payload)
    assert first.status_code == 202
    assert first.json()["created"] is True
    assert first.json()["job"]["spec"]["dataset_fingerprint"]
    constrained = WorkbenchSettings(
        parquet_root=tmp_path / "parquet",
        state_root=tmp_path / "state",
        resource_policy=ResourcePolicy(max_estimated_rows=1, min_available_memory_mib=0),
    )
    retry = TestClient(create_workbench_app(constrained)).post(
        "/api/v3/experiments", headers=headers, json=payload
    )
    assert retry.status_code == 202
    assert retry.json()["created"] is False
    assert retry.json()["job"]["job_id"] == first.json()["job"]["job_id"]
    listed = client.get("/api/v3/experiments", params={"limit": 1, "cursor": 0})
    assert listed.status_code == 200
    assert [item["job_id"] for item in listed.json()["items"]] == [first.json()["job"]["job_id"]]
    reproduced = client.post(
        f"/api/v3/experiments/{first.json()['job']['job_id']}/reproduce",
        headers={"Idempotency-Key": "reproduce-key"},
    )
    assert reproduced.status_code == 202
    assert reproduced.json()["job"]["spec"]["reproduced_from"] == first.json()["job"]["job_id"]
    assert reproduced.json()["job"]["spec"]["dataset_fingerprint"] == first.json()["job"]["spec"]["dataset_fingerprint"]
    assert client.get("/api/v3/results", params={"limit": 201}).status_code == 422
    assert client.get("/api/v3/artifacts", params={"limit": 1}).status_code == 200
    changed = json.loads(json.dumps(payload))
    changed["precision"] = "trade"
    assert client.post("/api/v3/experiments", headers=headers, json=changed).status_code == 409


def test_v3_rejects_unaccepted_perpetual_research_before_enqueue(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet" / "ohlcv"
    parquet_dir.mkdir(parents=True)
    index = pd.date_range("2025-01-01", periods=4, freq="1h", tz="UTC")
    pd.DataFrame(
        {
            "open": [100.0] * 4,
            "high": [101.0] * 4,
            "low": [99.0] * 4,
            "close": [100.0] * 4,
            "volume": [1.0] * 4,
        },
        index=index,
    ).to_parquet(parquet_dir / "fixture_BTCUSDT_1h.parquet")
    (parquet_dir / "fixture_BTCUSDT_1h.manifest.json").write_text(
        '{"provider":"fixture","symbol":"BTC/USDT","timeframe":"1h","market":"perpetual"}',
        encoding="utf-8",
    )
    client = TestClient(
        create_workbench_app(
            WorkbenchSettings(parquet_root=tmp_path / "parquet", state_root=tmp_path / "state"),
        ),
    )
    version_id = client.post(
        "/api/v3/strategies",
        json={
            "name": "guard fixture",
            "kind": "rules",
            "rules": {
                "entry": {
                    "kind": "signal",
                    "signal": {"indicator": "rsi", "timeframe": "1h"},
                    "comparator": "<",
                    "right": 30,
                },
                "exit": {
                    "kind": "signal",
                    "signal": {"indicator": "rsi", "timeframe": "1h"},
                    "comparator": ">",
                    "right": 55,
                },
            },
        },
    ).json()["version"]["version_id"]

    response = client.post(
        "/api/v3/experiments",
        headers={"Idempotency-Key": "perpetual-guard"},
        json={
            "strategy_version_id": version_id,
            "dataset_id": "fixture-btcusdt-1h",
            "market": "perpetual",
            "costs": {"leverage": 2},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_experiment"
    assert "quote/mark, margin, and liquidation acceptance" in response.json()["detail"]["message"]


def test_v3_edge_origin_credentials_and_dataset_precision_are_enforced(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet" / "ohlcv"
    parquet_dir.mkdir(parents=True)
    index = pd.date_range("2024-01-01", periods=20, freq="h", tz="UTC")
    pd.DataFrame(
        {
            "open": range(1, 21),
            "high": range(2, 22),
            "low": range(1, 21),
            "close": range(1, 21),
            "volume": [1.0] * 20,
        },
        index=index,
    ).to_parquet(parquet_dir / "okx_BTCUSDT_1h.parquet")
    settings = WorkbenchSettings(
        parquet_root=tmp_path / "parquet",
        state_root=tmp_path / "state",
        origin_client_id="edge-id",
        origin_client_secret="edge-secret",
        resource_policy=ResourcePolicy(max_estimated_rows=1_000_000, min_available_memory_mib=0),
    )
    client = TestClient(create_workbench_app(settings))
    assert client.get("/api/v3/capabilities").status_code == 401
    edge_headers = {
        "CF-Access-Client-Id": "edge-id",
        "CF-Access-Client-Secret": "edge-secret",
        "X-QT-Access-Subject": "researcher@example.test",
    }
    assert client.get("/api/v3/capabilities", headers=edge_headers).status_code == 200
    created = client.post(
        "/api/v3/strategies",
        headers=edge_headers,
        json={
            "name": "rule",
            "kind": "rules",
            "rules": {
                "entry": {
                    "kind": "signal",
                    "signal": {"indicator": "rsi", "timeframe": "1h"},
                    "comparator": "<",
                    "right": 30,
                },
                "exit": {
                    "kind": "signal",
                    "signal": {"indicator": "rsi", "timeframe": "1h"},
                    "comparator": ">",
                    "right": 55,
                },
            },
        },
    )
    version_id = created.json()["version"]["version_id"]
    response = client.post(
        "/api/v3/experiments",
        headers={**edge_headers, "Idempotency-Key": "trade-precision"},
        json={
            "strategy_version_id": version_id,
            "dataset_id": "okx-btcusdt-1h",
            "precision": "trade",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_experiment"


def test_v3_source_research_contract_and_completed_evidence_report(tmp_path: Path) -> None:
    client = TestClient(
        create_workbench_app(WorkbenchSettings(parquet_root=tmp_path / "parquet", state_root=tmp_path / "state"))
    )
    contracts = client.get("/api/v3/source-research/contracts")
    assert contracts.status_code == 200
    identifiers = {item["id"] for item in contracts.json()["items"]}
    assert "rank_catalog_backtests" in identifiers
    assert "create_crowding_fader_port" in identifiers

    report = client.post(
        "/api/v3/source-research/reports",
        json={"evolution_results": [], "walk_forward_results": [], "catalog_results": []},
    )
    assert report.status_code == 200
    payload = report.json()
    assert payload["sources"]["btcqt"]["commit"]
    assert payload["evolution"]["candidate_ids"]
