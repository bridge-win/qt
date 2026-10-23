"""Dataset identity derives from real bytes and point-in-time metadata."""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from qt.datasets import DatasetIntegrityError, DatasetRegistry


def register(registry: DatasetRegistry, table: pa.Table, **changes: object):
    arguments: dict[str, object] = {
        "venue": "fixture", "instrument": "BTCUSDT", "data_type": "trade",
        "timestamp_column": "exchange_ts_ns", "rights": "fixture only",
        "provenance": {"source": "unit-test"}, "availability": "reconstructed_available",
        "expected_interval_ns": 10,
    }
    return registry.register_table(table, **{**arguments, **changes})


def trades(times=(10, 20, 40)) -> pa.Table:
    return pa.table({"exchange_ts_ns": list(times), "price": ["100"] * len(times),
                     "quantity": ["1"] * len(times)})


def test_registered_snapshot_measures_rows_range_gaps_and_content(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path)
    manifest = register(registry, trades())
    assert manifest.rows == 3
    assert manifest.start_ns == 10 and manifest.end_ns == 40
    assert manifest.gaps[0].start_ns == manifest.gaps[0].end_ns == 30
    assert manifest.gaps[0].missing_count == 1
    assert registry.load(manifest.dataset_id) == manifest
    assert registry.read_table(manifest.dataset_id).to_pydict() == trades().to_pydict()
    assert register(registry, trades()).dataset_id == manifest.dataset_id
    revised = register(registry, trades((10, 20, 30)), revision_id="revised")
    assert revised.dataset_id != manifest.dataset_id
    assert registry.load(manifest.dataset_id) == manifest


@pytest.mark.parametrize("times", [(20, 10), (10, 10), (10, 25), (-10, 0)])
def test_cannot_sort_away_bad_time_order_or_regular_grid(tmp_path, times) -> None:
    registry = DatasetRegistry(tmp_path)
    with pytest.raises(DatasetIntegrityError):
        register(registry, trades(times))
    assert list((tmp_path / "manifests").iterdir()) == []


def test_event_stream_allows_equal_timestamps_without_expected_grid(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path)
    assert register(registry, trades((10, 10)), expected_interval_ns=None).rows == 2


@pytest.mark.parametrize("table", [
    pa.table({"exchange_ts_ns": pa.array([], type=pa.int64())}),
    pa.table({"exchange_ts_ns": [None, 20]}),
    pa.table({"exchange_ts_ns": [10.0, 20.0]}),
    pa.table({"exchange_ts_ns": [10, 20], "price": [float("nan"), 1.0]}),
    pa.table({"exchange_ts_ns": [10, 20], "price": [1.0, float("inf")]}),
])
def test_invalid_actual_parquet_data_does_not_publish_manifest(tmp_path, table) -> None:
    registry = DatasetRegistry(tmp_path)
    with pytest.raises(DatasetIntegrityError):
        register(registry, table)
    assert list((tmp_path / "manifests").iterdir()) == []


def test_manifest_cannot_lie_about_file_rows_even_if_rehashed(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path)
    manifest = register(registry, trades())
    path = tmp_path / "manifests" / f"{manifest.dataset_id}.json"
    payload = json.loads(path.read_text())
    payload["rows"] = payload["files"][0]["rows"] = 4
    path.write_text(json.dumps(payload))
    with pytest.raises(DatasetIntegrityError):
        registry.load(manifest.dataset_id)


def test_object_corruption_and_file_deletion_are_detected(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path)
    manifest = register(registry, trades())
    path = tmp_path / manifest.files[0].relative_path
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(DatasetIntegrityError, match="content hash"):
        registry.load(manifest.dataset_id)
    with pytest.raises(DatasetIntegrityError, match="corrupted"):
        register(registry, trades())
    path.unlink()
    with pytest.raises(DatasetIntegrityError, match="missing"):
        registry.load(manifest.dataset_id)


def test_traversal_and_symlink_escape_are_rejected(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path / "root")
    outside = tmp_path / "outside.parquet"
    pq.write_table(trades(), outside)
    for source in (outside, "../outside.parquet"):
        with pytest.raises(DatasetIntegrityError, match="escapes"):
            registry.register_parquet(source, venue="fixture", instrument="BTCUSDT", data_type="trade",
                                      timestamp_column="exchange_ts_ns", rights="test", provenance={})
    (registry.root / "escape.parquet").symlink_to(outside)
    with pytest.raises(DatasetIntegrityError, match="escapes"):
        registry.register_parquet("escape.parquet", venue="fixture", instrument="BTCUSDT",
                                  data_type="trade", timestamp_column="exchange_ts_ns",
                                  rights="test", provenance={})
    with pytest.raises(DatasetIntegrityError):
        registry.load("../../outside")


def test_arrow_timestamps_are_measured_in_ns(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path)
    table = pa.table({"time": pa.array([1, 2], type=pa.timestamp("ms", tz="UTC"))})
    manifest = register(registry, table, timestamp_column="time", expected_interval_ns=1_000_000)
    assert manifest.start_ns == 1_000_000 and manifest.end_ns == 2_000_000


def test_receipt_history_is_not_fabricated(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path)
    with pytest.raises(DatasetIntegrityError, match="lacks received"):
        register(registry, trades(), availability="received")
    reconstructed = register(registry, trades())
    events = list(registry.events(reconstructed.dataset_id))
    assert events[0].quality_flags == ("RECONSTRUCTED_RECEIPT",)
    assert events[0].exchange_ts_ns == 10


def test_open_stamped_bars_replay_only_at_close(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path)
    table = pa.table({"time": [0, 10], "open": ["100", "101"], "high": ["102", "103"],
                      "low": ["99", "100"], "close": ["101", "102"], "volume": ["1", "2"]})
    manifest = register(registry, table, timestamp_column="time", data_type="ohlcv",
                        provenance={"timestamp_semantics": "open"})
    events = list(registry.events(manifest.dataset_id))
    assert [event.exchange_ts_ns for event in events] == [10, 20]
    assert [event.received_ts_ns for event in events] == [10, 20]
    assert events[0].payload["open_ts_ns"] == 0
    missing_semantics = register(registry, table, timestamp_column="time", data_type="ohlcv")
    with pytest.raises(DatasetIntegrityError, match="semantics"):
        list(registry.events(missing_semantics.dataset_id))


def test_actual_trade_receipt_order_is_preserved(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path)
    table = pa.table({"received_ts_ns": [20, 30], "exchange_ts_ns": [10, 10],
                      "price": ["100", "101"], "quantity": ["1", "2"]})
    manifest = register(registry, table, timestamp_column="received_ts_ns", availability="received")
    assert [event.payload["price"] for event in registry.events(manifest.dataset_id)] == ["100", "101"]
