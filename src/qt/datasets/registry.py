"""Rooted content-addressed Parquet snapshots with verified manifests.

The manifest is derived from the copied Parquet bytes, not caller-provided
row counts or time ranges. Loading verifies both the manifest identity and
the full bytes plus temporal metadata before returning a usable snapshot.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import TypeAdapter

from qt.contracts import (
    DataGap,
    DatasetFile,
    DatasetManifest,
    FrozenDict,
    MarketEvent,
    canonical_json,
    content_hash,
    decimal_string,
)
from qt.contracts.base import NonNegativeDecimal, PositiveDecimal, TimestampNS
from qt.contracts.models import MarketEventType

_PRICE = TypeAdapter(PositiveDecimal)
_SIZE = TypeAdapter(NonNegativeDecimal)
_TIMESTAMP = TypeAdapter(TimestampNS)


class DatasetIntegrityError(ValueError):
    """An immutable object, manifest or its measured contents disagree."""


def _timestamp(value: object, name: str) -> int:
    try:
        return _TIMESTAMP.validate_python(value, strict=True)
    except ValueError as exc:
        raise DatasetIntegrityError(f"invalid integer nanosecond timestamp: {name}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_identity(value: DatasetManifest | Mapping[str, object]) -> str:
    fields = value.model_dump(mode="json") if isinstance(value, DatasetManifest) else dict(value)
    fields.pop("dataset_id", None)
    fields.setdefault("schema_version", "2.0")
    return content_hash(fields)


class DatasetRegistry:
    """Immutable single-host dataset registry; source paths stay within root.

    ``register_parquet`` imports an existing rooted file. ``register_table``
    writes a newly collected table. Both publish content-addressed objects
    atomically without replacing an existing version.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._directory("objects")
        self._directory("manifests")
        self._directory("staging")

    def _directory(self, relative_path: str) -> Path:
        directory = self._rooted(relative_path)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _rooted(self, path: Path | str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise DatasetIntegrityError("dataset path escapes the registry root")
        return resolved

    def register_parquet(
        self,
        path: Path | str,
        *,
        venue: str,
        instrument: str,
        data_type: str,
        timestamp_column: str,
        rights: str,
        provenance: Mapping[str, object],
        revision_id: str = "initial",
        availability: str = "received",
        expected_interval_ns: int | None = None,
    ) -> DatasetManifest:
        source = self._rooted(path)
        if not source.is_file():
            raise DatasetIntegrityError("dataset source is not a regular file")
        temporary = self._temporary()
        try:
            shutil.copyfile(source, temporary)
            return self._register(
                temporary, venue=venue, instrument=instrument, data_type=data_type,
                timestamp_column=timestamp_column, rights=rights, provenance=provenance,
                revision_id=revision_id, availability=availability,
                expected_interval_ns=expected_interval_ns,
            )
        finally:
            temporary.unlink(missing_ok=True)

    def register_table(
        self,
        table: pa.Table,
        *,
        venue: str,
        instrument: str,
        data_type: str,
        timestamp_column: str,
        rights: str,
        provenance: Mapping[str, object],
        revision_id: str = "initial",
        availability: str = "received",
        expected_interval_ns: int | None = None,
    ) -> DatasetManifest:
        temporary = self._temporary()
        try:
            pq.write_table(table, temporary, compression="zstd")
            return self._register(
                temporary, venue=venue, instrument=instrument, data_type=data_type,
                timestamp_column=timestamp_column, rights=rights, provenance=provenance,
                revision_id=revision_id, availability=availability,
                expected_interval_ns=expected_interval_ns,
            )
        finally:
            temporary.unlink(missing_ok=True)

    def _temporary(self) -> Path:
        descriptor, name = tempfile.mkstemp(suffix=".parquet", dir=self._directory("staging"))
        os.close(descriptor)
        return Path(name)

    def _register(self, temporary: Path, **metadata: Any) -> DatasetManifest:
        measured = self._inspect(
            temporary, metadata["timestamp_column"], metadata["expected_interval_ns"],
            availability=metadata["availability"],
            event_identity=((metadata["venue"], metadata["instrument"])
                            if metadata["data_type"] == "market_events" else None),
        )
        digest = _sha256(temporary)
        relative_path = f"objects/{digest}.parquet"
        part = DatasetFile(
            relative_path=relative_path, sha256=digest, bytes=temporary.stat().st_size,
            rows=measured["rows"], start_ns=measured["start_ns"], end_ns=measured["end_ns"],
        )
        payload = {**metadata, **{key: measured[key] for key in (
            "rows", "start_ns", "end_ns", "gaps", "columns", "arrow_schema",
        )}, "schema_version": "2.0", "files": (part,)}
        draft = DatasetManifest(dataset_id="0" * 64, **payload)
        manifest = DatasetManifest.model_validate({
            **draft.model_dump(mode="python"), "dataset_id": manifest_identity(draft),
        })
        self._publish(temporary, self._rooted(relative_path), expected_hash=digest)
        self._publish_manifest(manifest)
        return manifest

    def _publish(self, source: Path, destination: Path, *, expected_hash: str) -> None:
        with source.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            os.link(source, destination)
        except FileExistsError:
            if _sha256(destination) != expected_hash:
                raise DatasetIntegrityError("existing immutable object is corrupted") from None
        self._sync_directory(destination.parent)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _publish_manifest(self, manifest: DatasetManifest) -> None:
        path = self._rooted(f"manifests/{manifest.dataset_id}.json")
        descriptor, name = tempfile.mkstemp(dir=self._directory("staging"))
        temporary = Path(name)
        try:
            data = canonical_json(manifest).encode("utf-8")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._publish(temporary, path, expected_hash=hashlib.sha256(data).hexdigest())
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, dataset_id: str) -> DatasetManifest:
        if not re.fullmatch(r"[a-f0-9]{64}", dataset_id):
            raise DatasetIntegrityError("dataset ID must be a SHA-256 identity")
        path = self._rooted(f"manifests/{dataset_id}.json")
        try:
            manifest = DatasetManifest.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise DatasetIntegrityError("dataset manifest is missing or invalid") from exc
        if manifest.dataset_id != dataset_id:
            raise DatasetIntegrityError("manifest does not match the requested dataset")
        return self.verify(manifest)

    def verify(self, manifest: DatasetManifest) -> DatasetManifest:
        if manifest_identity(manifest) != manifest.dataset_id:
            raise DatasetIntegrityError("dataset manifest content hash mismatch")
        observed_rows = 0
        observed_gaps: list[DataGap] = []
        previous_end: int | None = None
        previous_received: int | None = None
        for part in manifest.files:
            path = self._rooted(part.relative_path)
            try:
                if path.stat().st_size != part.bytes or _sha256(path) != part.sha256:
                    raise DatasetIntegrityError("dataset object content hash mismatch")
                measured = self._inspect(
                    path, manifest.timestamp_column, manifest.expected_interval_ns,
                    availability=manifest.availability,
                    event_identity=((manifest.venue, manifest.instrument)
                                    if manifest.data_type == "market_events" else None),
                )
            except OSError as exc:
                raise DatasetIntegrityError("dataset object is missing or unreadable") from exc
            for key in ("rows", "start_ns", "end_ns"):
                if measured[key] != getattr(part, key):
                    raise DatasetIntegrityError(f"dataset object measured {key} mismatch")
            if measured["columns"] != manifest.columns or measured["arrow_schema"] != manifest.arrow_schema:
                raise DatasetIntegrityError("dataset object schema mismatch")
            if previous_end is not None:
                observed_gaps.extend(self._gap(previous_end, measured["start_ns"],
                                               manifest.expected_interval_ns))
            previous_end = measured["end_ns"]
            if manifest.availability == "received":
                if (previous_received is not None
                        and measured["received_start_ns"] < previous_received):
                    raise DatasetIntegrityError("actual receipt history is out of order across parts")
                previous_received = measured["received_end_ns"]
            observed_rows += measured["rows"]
            observed_gaps.extend(measured["gaps"])
        if observed_rows != manifest.rows or tuple(observed_gaps) != manifest.gaps:
            raise DatasetIntegrityError("dataset measured rows or gaps mismatch")
        return manifest

    def read_table(self, dataset_id: str) -> pa.Table:
        manifest = self.load(dataset_id)
        return pa.concat_tables([
            pq.ParquetFile(self._rooted(part.relative_path)).read() for part in manifest.files
        ])

    def events(self, dataset_id: str) -> Iterator[MarketEvent]:
        """Replay verified rows in their stored order as typed market events.

        Bar timestamps must declare ``provenance.timestamp_semantics`` as
        ``open`` or ``close`` unless explicit ``close_ts_ns`` is present.
        Open-stamped bars need ``expected_interval_ns``; they become observable
        at close, never at open. Missing received timestamps are reconstructed
        and flagged, rather than presented as genuine local receipt history.
        """
        manifest = self.load(dataset_id)
        kinds: dict[str, MarketEventType] = {
            "ohlcv": "bar", "bars": "bar", "bar": "bar", "trades": "trade",
            "trade": "trade", "snapshot": "snapshot", "delta": "delta",
            "quote": "quote", "funding": "funding", "liquidation": "liquidation",
        }
        kind = kinds.get(manifest.data_type)
        if kind is None and manifest.data_type != "market_events":
            raise DatasetIntegrityError("dataset type has no approved MarketEvent mapping")
        for part in manifest.files:
            parquet = pq.ParquetFile(self._rooted(part.relative_path))
            for batch in parquet.iter_batches(batch_size=64 if manifest.data_type == "market_events" else 65_536):
                for index, field in enumerate(batch.schema):
                    if pa.types.is_timestamp(field.type):
                        values = pc.cast(batch.column(index), pa.timestamp("ns", tz=field.type.tz))
                        batch = batch.set_column(index, field.name, pc.cast(values, pa.int64()))
                for row in batch.to_pylist():
                    if manifest.data_type == "market_events":
                        yield self._event_envelope(row, (manifest.venue, manifest.instrument))
                        continue
                    timestamp = _timestamp(row[manifest.timestamp_column], manifest.timestamp_column)
                    exchange_ns = _timestamp(row.get("exchange_ts_ns", timestamp), "exchange_ts_ns")
                    payload = {key: value for key, value in row.items()
                               if key not in {manifest.timestamp_column, "exchange_ts_ns",
                                              "received_ts_ns", "available_at_ns", "sequence"}}
                    if kind == "bar":
                        semantics = manifest.provenance.get("timestamp_semantics")
                        if "close_ts_ns" in row:
                            exchange_ns = _timestamp(row["close_ts_ns"], "close_ts_ns")
                        elif semantics == "open" and manifest.expected_interval_ns is not None:
                            exchange_ns = timestamp + manifest.expected_interval_ns
                            payload["open_ts_ns"] = timestamp
                            payload["close_ts_ns"] = exchange_ns
                        elif semantics == "close" or manifest.timestamp_column == "close_ts_ns":
                            exchange_ns = timestamp
                            payload["close_ts_ns"] = exchange_ns
                        else:
                            raise DatasetIntegrityError("bar close-time semantics are required")
                        if not {"open", "high", "low", "close", "volume"} <= row.keys():
                            raise DatasetIntegrityError("bar dataset requires OHLCV columns")
                        if "open_ts_ns" not in payload and manifest.expected_interval_ns is not None:
                            payload["open_ts_ns"] = exchange_ns - manifest.expected_interval_ns
                    for name in ("open", "high", "low", "close", "price", "bid", "ask"):
                        if name in payload:
                            raw = payload[name]
                            # Historical numeric Arrow columns are normalized
                            # at ingestion, while execution DTOs still reject
                            # callers passing binary floats as money.
                            text = str(raw) if type(raw) in (int, float) else raw
                            try:
                                payload[name] = decimal_string(_PRICE.validate_python(text, strict=True))
                            except ValueError as exc:
                                raise DatasetIntegrityError(f"invalid positive financial column: {name}") from exc
                    for name in ("volume", "quantity", "size", "buy_volume", "sell_volume"):
                        if name in payload:
                            raw = payload[name]
                            text = str(raw) if type(raw) in (int, float) else raw
                            try:
                                payload[name] = decimal_string(_SIZE.validate_python(text, strict=True))
                            except ValueError as exc:
                                raise DatasetIntegrityError(f"invalid nonnegative financial column: {name}") from exc
                    available_ns = _timestamp(row.get("available_at_ns", exchange_ns), "available_at_ns")
                    received_ns = _timestamp(row.get("received_ts_ns", available_ns), "received_ts_ns")
                    if received_ns < available_ns:
                        raise DatasetIntegrityError("event version is received before its availability")
                    if kind == "bar" and (available_ns < exchange_ns or received_ns < available_ns):
                        raise DatasetIntegrityError("bar is available before its close")
                    if kind == "bar":
                        prices = [_PRICE.validate_python(payload[name], strict=True)
                                  for name in ("low", "open", "close", "high")]
                        if not prices[0] <= min(prices[1:3]) <= max(prices[1:3]) <= prices[3]:
                            raise DatasetIntegrityError("bar OHLC prices are inconsistent")
                        if ("open_ts_ns" in payload
                                and (type(payload["open_ts_ns"]) is not int
                                     or not 0 <= payload["open_ts_ns"] < exchange_ns)):
                            raise DatasetIntegrityError("bar open must precede its close")
                    reconstructed = "received_ts_ns" not in row
                    if manifest.availability == "received" and reconstructed:
                        raise DatasetIntegrityError("actual-receipt dataset lacks received_ts_ns")
                    assert kind is not None
                    yield MarketEvent(
                        venue=manifest.venue, instrument=manifest.instrument, event_type=kind,
                        exchange_ts_ns=exchange_ns, received_ts_ns=received_ns,
                        available_at_ns=available_ns, sequence=row.get("sequence"), payload=FrozenDict(payload),
                        quality_flags=("RECONSTRUCTED_RECEIPT",) if reconstructed else (),
                        revision_id=manifest.revision_id, source_hash=part.sha256,
                    )

    @staticmethod
    def _event_envelope(row: Mapping[str, Any], identity: tuple[str, str]) -> MarketEvent:
        try:
            encoded = row["event_json"]
            if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > 4 * 1024 * 1024:
                raise DatasetIntegrityError("event envelope JSON is not bounded UTF-8 text")
            event = MarketEvent.model_validate_json(encoded)
            if (event.venue != identity[0] or event.instrument != identity[1]
                    or event.received_ts_ns != row["received_ts_ns"]
                    or event.exchange_ts_ns != row["exchange_ts_ns"]
                    or event.event_type != row["event_type"] or event.sequence != row["sequence"]):
                raise DatasetIntegrityError("event envelope disagrees with its immutable row")
            return event
        except (ValueError, KeyError, TypeError) as exc:
            raise DatasetIntegrityError("event envelope is malformed or disagrees with its immutable row") from exc

    @staticmethod
    def _gap(previous: int, current: int, interval_ns: int | None) -> list[DataGap]:
        if current < previous:
            raise DatasetIntegrityError("timestamps are out of order; sorting would hide a gap")
        if interval_ns is None:
            return []
        delta = current - previous
        if delta <= 0 or delta % interval_ns:
            raise DatasetIntegrityError("regular-grid timestamps are duplicate or off-grid")
        if delta > interval_ns:
            return [DataGap(start_ns=previous + interval_ns, end_ns=current - interval_ns,
                            missing_count=delta // interval_ns - 1,
                            reason="missing_expected_timestamp")]
        return []

    @classmethod
    def _inspect(
        cls, path: Path, timestamp_column: str, interval_ns: int | None,
        *, availability: str, event_identity: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        if interval_ns is not None and (type(interval_ns) is not int or interval_ns <= 0):
            raise DatasetIntegrityError("expected interval must be positive integer nanoseconds")
        try:
            parquet = pq.ParquetFile(path)
            schema = parquet.schema_arrow.remove_metadata()
            if len(set(schema.names)) != len(schema.names):
                raise DatasetIntegrityError("duplicate parquet column names")
            field = schema.field(timestamp_column)
            if not (pa.types.is_integer(field.type) or pa.types.is_timestamp(field.type)):
                raise DatasetIntegrityError("timestamp column must be integer ns or Arrow timestamp")
            if availability == "received" and "received_ts_ns" not in schema.names:
                raise DatasetIntegrityError("actual-receipt dataset lacks received_ts_ns")
            if event_identity is not None and not {
                "event_json", "received_ts_ns", "exchange_ts_ns", "event_type", "sequence",
            } <= set(schema.names):
                raise DatasetIntegrityError("market event envelopes require all identity and timestamp columns")
            rows, first, previous = 0, None, None
            first_received: int | None = None
            previous_received: int | None = None
            gaps: list[DataGap] = []
            for batch in parquet.iter_batches(batch_size=64 if event_identity is not None else 65_536):
                times = batch.column(batch.schema.get_field_index(timestamp_column))
                if times.null_count:
                    raise DatasetIntegrityError("timestamps contain nulls")
                if pa.types.is_timestamp(times.type):
                    times = pc.cast(times, pa.timestamp("ns", tz=times.type.tz))
                times = pc.cast(times, pa.int64(), safe=True)
                for current in times.to_pylist():
                    if current < 0:
                        raise DatasetIntegrityError("timestamps precede the Unix epoch")
                    if previous is not None:
                        gaps.extend(cls._gap(previous, current, interval_ns))
                    else:
                        first = current
                    previous = current
                    rows += 1
                if availability == "received":
                    received = batch.column(batch.schema.get_field_index("received_ts_ns"))
                    if received.null_count or not pa.types.is_integer(received.type):
                        raise DatasetIntegrityError("actual receipt timestamps require nonnull integer ns")
                    received = pc.cast(received, pa.int64(), safe=True)
                    for received_ns in received.to_pylist():
                        if received_ns < 0 or (previous_received is not None and received_ns < previous_received):
                            raise DatasetIntegrityError("actual receipt history is out of order")
                        previous_received = received_ns
                        if first_received is None:
                            first_received = received_ns
                if event_identity is not None:
                    for row in batch.to_pylist():
                        cls._event_envelope(row, event_identity)
                for column in batch.columns:
                    if pa.types.is_floating(column.type):
                        finite = pc.all(pc.fill_null(pc.is_finite(column), True)).as_py()
                        if finite is False:
                            raise DatasetIntegrityError("numeric data contains NaN or Infinity")
            if not rows or first is None or previous is None:
                raise DatasetIntegrityError("empty datasets cannot be registered")
            return {"rows": rows, "start_ns": first, "end_ns": previous,
                    "gaps": tuple(gaps), "columns": tuple(schema.names),
                    "arrow_schema": schema.to_string(), "received_start_ns": first_received,
                    "received_end_ns": previous_received}
        except (pa.ArrowException, KeyError, TypeError) as exc:
            raise DatasetIntegrityError("Parquet contents or timestamp schema are invalid") from exc


def export_manifest(manifest: DatasetManifest) -> dict[str, Any]:
    """Transport helper; the returned copy cannot mutate the registered manifest."""
    return manifest.model_dump(mode="json")
