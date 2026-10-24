"""Dataset identity derives from real bytes and point-in-time metadata."""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from qt.contracts import DatasetManifest, MarketEvent
from qt.datasets import DatasetIntegrityError, DatasetRegistry
from qt.datasets.registry import manifest_identity


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


@pytest.mark.parametrize("change", [{"received_ts_ns": 99}, {"exchange_ts_ns": 99},
    {"event_type": "delta"}, {"sequence": 99}, {"event_json": "null"},
])
def test_invalid_event_envelopes_are_rejected_before_publishing(tmp_path, change) -> None:
    registry = DatasetRegistry(tmp_path)
    event = MarketEvent(venue="fixture", instrument="BTCUSDT", event_type="trade",
                        exchange_ts_ns=1, received_ts_ns=2, sequence=1)
    row = {"received_ts_ns": 2, "exchange_ts_ns": 1, "event_type": "trade",
           "sequence": 1, "event_json": event.model_dump_json(), **change}
    with pytest.raises(DatasetIntegrityError, match="envelope"):
        register(registry, pa.Table.from_pylist([row]), data_type="market_events",
                 timestamp_column="received_ts_ns", expected_interval_ns=None, availability="received")
    assert not list((tmp_path / "manifests").iterdir())


def test_event_envelope_columns_and_secondary_receipt_integer_capacity_are_verified(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path)
    with pytest.raises(DatasetIntegrityError, match="require all"):
        register(registry, pa.table({"received_ts_ns": [1]}), data_type="market_events",
                 timestamp_column="received_ts_ns", availability="received")
    with pytest.raises(DatasetIntegrityError):
        register(registry, pa.table({"exchange_ts_ns": [1],
                                    "received_ts_ns": pa.array([2**63], type=pa.uint64())}),
                 availability="received")


def test_multipart_snapshot_cannot_hide_receipt_order_regression(tmp_path) -> None:
    registry = DatasetRegistry(tmp_path)
    parts = [register(registry, pa.table({"exchange_ts_ns": [exchange], "received_ts_ns": [received],
                                        "price": ["100"], "quantity": ["1"]}), availability="received")
             for exchange, received in ((10, 100), (20, 90))]
    fields = {**parts[0].model_dump(mode="python"), "files": tuple(item.files[0] for item in parts),
              "rows": 2, "end_ns": 20}
    fields["dataset_id"] = manifest_identity(fields)
    combined = DatasetManifest.model_validate(fields)
    with pytest.raises(DatasetIntegrityError, match="across parts"):
        registry.verify(combined)


@pytest.mark.parametrize("change", [{"high": ["99"]}, {"open_ts_ns": [10]}, {"low": ["NaN"]}])
def test_replayed_bars_reject_inconsistent_ohlc_or_noncausal_interval(tmp_path, change) -> None:
    registry = DatasetRegistry(tmp_path)
    table = pa.table({"time": [10], "open": ["100"], "high": ["102"], "low": ["99"],
                      "close": ["101"], "volume": ["1"], **change})
    manifest = register(registry, table, timestamp_column="time", data_type="ohlcv",
                        provenance={"timestamp_semantics": "close"})
    with pytest.raises(DatasetIntegrityError):
        list(registry.events(manifest.dataset_id))


@pytest.mark.parametrize("change", [{"available_at_ns": [None]}, {"available_at_ns": [30]},
                                   {"exchange_ts_ns": [None]}])
def test_invalid_secondary_times_cannot_create_usable_events(tmp_path, change) -> None:
    registry = DatasetRegistry(tmp_path)
    manifest = register(registry, pa.table({"time": [1], "exchange_ts_ns": [10],
                                            "received_ts_ns": [20], "available_at_ns": [10],
                                            "price": ["100"], "quantity": ["1"], **change}),
                        timestamp_column="time", expected_interval_ns=None, availability="received")
    with pytest.raises(DatasetIntegrityError):
        list(registry.events(manifest.dataset_id))
