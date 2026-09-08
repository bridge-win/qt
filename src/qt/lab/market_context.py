"""Bounded, attested OHLCV and fill context for immutable research results."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

import pandas as pd
import pyarrow.parquet as pq

from qt.lab.persistence import LabRepository
from qt.research.datasets import DatasetCatalog

JsonDict: TypeAlias = dict[str, object]

MAX_CONTEXT_BARS = 2_000
MAX_FILL_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_FILL_MARKERS = 5_000
_RESULT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_TIMEFRAME = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>[mhdw])$")


class MarketContextError(ValueError):
    """A result cannot safely provide historical market context."""


class MarketContextWindowError(MarketContextError):
    """The caller supplied invalid or unbounded time limits."""


class DatasetFingerprintMismatchError(MarketContextError):
    """The local dataset no longer matches an immutable result."""


class FillArtifactUnavailableError(MarketContextError):
    """The attested native fills report cannot be read safely."""


@dataclass(frozen=True)
class MarketContextWindow:
    start: datetime
    end: datetime

    @classmethod
    def parse(cls, from_time: str | None, to_time: str | None) -> MarketContextWindow:
        if from_time is None or to_time is None:
            raise MarketContextWindowError("from and to must both be supplied")
        start = _parse_aware_timestamp(from_time, field="from")
        end = _parse_aware_timestamp(to_time, field="to")
        if start > end:
            raise MarketContextWindowError("from must not be later than to")
        return cls(start=start, end=end)


class VerifiedArtifactResolver:
    """Resolve only result-owned, attested native artifacts below one private root."""

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root.resolve()

    def read_fills(self, result_id: str, summary: Mapping[str, object]) -> tuple[bytes, JsonDict]:
        if not _RESULT_ID.fullmatch(result_id):
            raise FillArtifactUnavailableError("result identifier is not safe for artifact lookup")
        descriptor = _fills_descriptor(summary)
        declared_size = descriptor.get("size_bytes")
        declared_hash = descriptor.get("sha256")
        if not isinstance(declared_size, int) or not 0 <= declared_size <= MAX_FILL_ARTIFACT_BYTES:
            raise FillArtifactUnavailableError("fills.csv has no bounded attested size")
        if not isinstance(declared_hash, str) or not _FINGERPRINT.fullmatch(declared_hash):
            raise FillArtifactUnavailableError("fills.csv has no valid attested SHA-256")
        path = self.artifact_root / result_id / "fills.csv"
        content = _read_attested_file(path, root=self.artifact_root, maximum=declared_size)
        if len(content) != declared_size:
            raise FillArtifactUnavailableError("fills.csv size does not match its immutable result")
        if hashlib.sha256(content).hexdigest() != declared_hash:
            raise FillArtifactUnavailableError("fills.csv SHA-256 does not match its immutable result")
        return content, descriptor


class MarketContextReader:
    """Read source-verified candles and actual fill markers without worker ownership."""

    def __init__(self, *, repository: LabRepository, parquet_root: Path, artifact_root: Path) -> None:
        self.repository = repository
        self.datasets = DatasetCatalog(parquet_root)
        self.artifacts = VerifiedArtifactResolver(artifact_root)

    def read(self, result_id: str, window: MarketContextWindow) -> JsonDict:
        result = self.repository.get_result(result_id)
        summary = _result_summary(result)
        dataset = self._resolve_dataset(summary)
        bars = _read_candles(self.datasets.path_for(_required_text(dataset, "dataset_id")), window)
        if len(bars) > MAX_CONTEXT_BARS:
            raise MarketContextWindowError(
                f"market-context window contains {len(bars)} bars; maximum is {MAX_CONTEXT_BARS}"
            )
        interval = _timeframe_delta(_required_text(dataset, "timeframe"))
        content, descriptor = self.artifacts.read_fills(result_id, summary)
        fills = _fill_markers(content, window)
        if len(fills) > MAX_FILL_MARKERS:
            raise FillArtifactUnavailableError(
                f"fills.csv contains more than {MAX_FILL_MARKERS} markers in the requested window"
            )
        return {
            "result_id": result_id,
            "dataset": {
                "dataset_id": dataset["dataset_id"],
                "symbol": dataset["symbol"],
                "timeframe": dataset["timeframe"],
                "fingerprint": dataset["fingerprint"],
            },
            "window": {
                "from": window.start.isoformat(),
                "to": window.end.isoformat(),
                "bar_count": len(bars),
                "limit": MAX_CONTEXT_BARS,
            },
            "candles": [
                {
                    "time": timestamp.isoformat(),
                    "available_at": (timestamp + interval).isoformat(),
                    "open": values["open"],
                    "high": values["high"],
                    "low": values["low"],
                    "close": values["close"],
                    "volume": values["volume"],
                }
                for timestamp, values in bars
            ],
            "fills": fills,
            "fill_artifact": {
                "name": "fills.csv",
                "sha256": descriptor["sha256"],
                "rows": descriptor.get("rows"),
            },
        }

    def _resolve_dataset(self, summary: Mapping[str, object]) -> JsonDict:
        data = summary.get("data")
        if not isinstance(data, Mapping):
            raise DatasetFingerprintMismatchError("result has no immutable data identity")
        expected = data.get("fingerprint")
        if not isinstance(expected, str) or not _FINGERPRINT.fullmatch(expected):
            raise DatasetFingerprintMismatchError("result has no valid dataset fingerprint")
        symbol = data.get("symbol")
        timeframe = data.get("timeframe")
        candidates = [
            dataset
            for dataset in self.datasets.list_datasets()
            if dataset.get("status") == "ready"
            and dataset.get("fingerprint") == expected
            and dataset.get("symbol") == symbol
            and dataset.get("timeframe") == timeframe
        ]
        if len(candidates) != 1:
            raise DatasetFingerprintMismatchError(
                "the configured parquet catalog does not contain exactly one dataset matching this result fingerprint"
            )
        return candidates[0]


def _result_summary(result: Mapping[str, object]) -> Mapping[str, object]:
    summary = result.get("summary")
    if not isinstance(summary, Mapping):
        raise DatasetFingerprintMismatchError("result summary is unavailable")
    return summary


def _parse_aware_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MarketContextWindowError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketContextWindowError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _timeframe_delta(value: str) -> pd.Timedelta:
    matched = _TIMEFRAME.fullmatch(value)
    if matched is None:
        raise DatasetFingerprintMismatchError("result timeframe is not supported for market-context")
    count = int(matched.group("count"))
    unit = matched.group("unit")
    return pd.Timedelta(**{"minutes" if unit == "m" else "hours" if unit == "h" else "days" if unit == "d" else "weeks": count})


def _read_candles(path: Path, window: MarketContextWindow) -> list[tuple[datetime, JsonDict]]:
    try:
        parquet = pq.ParquetFile(path)
    except (OSError, ValueError, TypeError) as error:
        raise DatasetFingerprintMismatchError("matched parquet dataset cannot be opened") from error
    names = set(parquet.schema.names)
    timestamp_column = next((item for item in ("__index_level_0__", "timestamp", "time") if item in names), None)
    required = {"open", "high", "low", "close", "volume"}
    if timestamp_column is None or not required.issubset(names):
        raise DatasetFingerprintMismatchError("matched parquet dataset does not contain canonical OHLCV columns")
    candles: list[tuple[datetime, JsonDict]] = []
    previous: datetime | None = None
    try:
        batches = parquet.iter_batches(
            batch_size=2048,
            columns=[timestamp_column, "open", "high", "low", "close", "volume"],
        )
        for batch in batches:
            frame = batch.to_pandas()
            for row in frame.itertuples(index=False, name=None):
                timestamp = _coerce_timestamp(row[0], field="candle timestamp")
                if timestamp < window.start or timestamp > window.end:
                    continue
                if previous is not None and timestamp <= previous:
                    raise DatasetFingerprintMismatchError("matched parquet timestamps are not strictly ordered")
                values = dict(zip(("open", "high", "low", "close", "volume"), row[1:], strict=True))
                candle = {key: _finite_number(value, field=key) for key, value in values.items()}
                if candle["low"] > min(candle["open"], candle["close"], candle["high"]) or candle["high"] < max(candle["open"], candle["close"], candle["low"]):
                    raise DatasetFingerprintMismatchError("matched parquet contains an invalid OHLC wick")
                previous = timestamp
                candles.append((timestamp, candle))
                if len(candles) > MAX_CONTEXT_BARS:
                    raise MarketContextWindowError(
                        f"market-context window contains more than {MAX_CONTEXT_BARS} bars"
                    )
    except MarketContextError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise DatasetFingerprintMismatchError("matched parquet dataset cannot be read") from error
    return candles


def _coerce_timestamp(value: object, *, field: str) -> datetime:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as error:
        raise DatasetFingerprintMismatchError(f"{field} is invalid") from error
    if timestamp.tz is None:
        raise DatasetFingerprintMismatchError(f"{field} must be timezone-aware")
    return timestamp.tz_convert("UTC").to_pydatetime()


def _finite_number(value: object, *, field: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise DatasetFingerprintMismatchError(f"candle {field} is not numeric") from error
    if not pd.notna(numeric) or not float("-inf") < numeric < float("inf"):
        raise DatasetFingerprintMismatchError(f"candle {field} is not finite")
    return numeric


def _fills_descriptor(summary: Mapping[str, object]) -> JsonDict:
    artifacts = summary.get("artifacts")
    if not isinstance(artifacts, list):
        raise FillArtifactUnavailableError("result has no attested native artifacts")
    descriptors = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, Mapping) and artifact.get("name") == "fills.csv"
    ]
    if len(descriptors) != 1:
        raise FillArtifactUnavailableError("result has no unique attested fills.csv artifact")
    return dict(descriptors[0])


def _read_attested_file(path: Path, *, root: Path, maximum: int) -> bytes:
    _assert_safe_artifact_path(path, root)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FillArtifactUnavailableError("attested fills.csv is unavailable") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise FillArtifactUnavailableError("attested fills.csv is not a bounded regular file")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(content)))
            if not chunk:
                return bytes(content)
            content.extend(chunk)
        raise FillArtifactUnavailableError("attested fills.csv exceeds its declared size")
    finally:
        os.close(descriptor)


def _assert_safe_artifact_path(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise FillArtifactUnavailableError("artifact path escapes the configured artifact root") from error
    current = root
    for component in relative.parts:
        try:
            info = current.lstat()
        except OSError as error:
            raise FillArtifactUnavailableError("attested artifact path is unavailable") from error
        if stat.S_ISLNK(info.st_mode):
            raise FillArtifactUnavailableError("attested artifact path may not traverse symlinks")
        current /= component
    try:
        info = current.lstat()
    except OSError as error:
        raise FillArtifactUnavailableError("attested fills.csv is unavailable") from error
    if stat.S_ISLNK(info.st_mode):
        raise FillArtifactUnavailableError("attested fills.csv may not be a symlink")


def _fill_markers(content: bytes, window: MarketContextWindow) -> list[JsonDict]:
    try:
        rows = csv.DictReader(io.StringIO(content.decode("utf-8")))
        required = {"side", "quantity", "avg_px", "ts_last"}
        if rows.fieldnames is None or not required.issubset(rows.fieldnames):
            raise FillArtifactUnavailableError("fills.csv does not contain native fill columns")
        markers: list[JsonDict] = []
        for row in rows:
            timestamp = _parse_aware_timestamp(str(row.get("ts_last", "")), field="fill timestamp")
            if timestamp < window.start or timestamp > window.end:
                continue
            side = row.get("side")
            if side not in {"BUY", "SELL"}:
                raise FillArtifactUnavailableError("fills.csv contains an invalid native fill side")
            markers.append(
                {
                    "time": timestamp.isoformat(),
                    "side": side,
                    "quantity": _finite_number(row.get("filled_qty") or row.get("quantity"), field="fill quantity"),
                    "price": _finite_number(row.get("avg_px"), field="fill price"),
                    "order_id": row.get("venue_order_id") or None,
                }
            )
            if len(markers) > MAX_FILL_MARKERS:
                raise FillArtifactUnavailableError(
                    f"fills.csv contains more than {MAX_FILL_MARKERS} markers in the requested window"
                )
    except UnicodeDecodeError as error:
        raise FillArtifactUnavailableError("fills.csv is not UTF-8 CSV") from error
    return markers


def _required_text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise DatasetFingerprintMismatchError(f"dataset {key} is unavailable")
    return item
