"""Truthful managed and local OHLCV dataset catalog."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

import httpx
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from btc_backtest.data.models import DataRequest
from btc_backtest.data.providers import BitstampProvider

JsonDict: TypeAlias = dict[str, object]


@dataclass(frozen=True)
class DatasetDefinition:
    dataset_id: str
    key: str
    provider: str
    symbol: str
    timeframe: str
    market: str = "spot"
    feed_types: tuple[str, ...] = ("bar",)
    standard: bool = False


MANAGED_DATASETS = (
    DatasetDefinition(
        dataset_id="bitstamp-btcusd-1d-10y",
        key="bitstamp_BTCUSD_1d",
        provider="bitstamp",
        symbol="BTC/USD",
        timeframe="1d",
        standard=True,
    ),
    DatasetDefinition(
        dataset_id="okx-btcusdt-1h",
        key="okx_BTCUSDT_1h",
        provider="okx",
        symbol="BTC/USDT",
        timeframe="1h",
    ),
)


class DatasetCatalog:
    def __init__(
        self,
        parquet_root: Path,
        *,
        syncing_ids: set[str] | None = None,
    ) -> None:
        self.parquet_root = parquet_root
        self.syncing_ids = syncing_ids or set()

    def list_datasets(self) -> list[JsonDict]:
        datasets: list[JsonDict] = []
        known_keys: set[str] = set()
        for definition in MANAGED_DATASETS:
            known_keys.add(definition.key)
            datasets.append(self._describe(definition))
        ohlcv_dir = self.parquet_root / "ohlcv"
        if ohlcv_dir.exists():
            for path in sorted(ohlcv_dir.glob("*.parquet")):
                if path.stem in known_keys:
                    continue
                described = self._describe_local(path)
                if described is not None:
                    datasets.append(described)
        return datasets

    def get(self, dataset_id: str) -> JsonDict:
        for definition in MANAGED_DATASETS:
            if definition.dataset_id == dataset_id:
                return self._describe(definition)
        local_path = self._local_path_for_id(dataset_id)
        if local_path is not None:
            described = self._describe_local(local_path)
            if described is not None:
                return described
        raise KeyError(dataset_id)

    def path_for(self, dataset_id: str) -> Path:
        dataset = self.get(dataset_id)
        if dataset["status"] != "ready":
            raise ValueError(f"dataset is not ready: {dataset_id}")
        return self.parquet_root / "ohlcv" / f"{dataset['key']}.parquet"

    def _describe(self, definition: DatasetDefinition) -> JsonDict:
        path = self.parquet_root / "ohlcv" / f"{definition.key}.parquet"
        base: JsonDict = {
            "dataset_id": definition.dataset_id,
            "key": definition.key,
            "provider": definition.provider,
            "symbol": definition.symbol,
            "timeframe": definition.timeframe,
            "market": definition.market,
            "feed_types": list(definition.feed_types),
            # This catalog currently manages OHLCV parquet only.  A request
            # for trades or an order book must name a dataset that declares
            # that precision; it may not silently run on bars.
            "precisions": ["bar"],
            "standard": definition.standard,
            "standard_ready": False,
            "status": "missing",
            "rows": 0,
            "start": None,
            "end": None,
            "fingerprint": None,
            "gaps": None,
            "retrieved_at": None,
            "freshness_status": "unknown",
            "source": (
                "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
                if definition.provider == "bitstamp"
                else "local parquet imported from OKX public candles"
            ),
        }
        inspected = _inspect_parquet(path)
        if inspected is None:
            if definition.dataset_id in self.syncing_ids:
                base["status"] = "syncing"
            return base
        base.update(inspected)
        if definition.dataset_id in self.syncing_ids:
            base["status"] = "syncing"
            return base
        manifest_path = path.with_suffix(".manifest.json")
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                base["retrieved_at"] = manifest.get("retrieved_at")
                base["gaps"] = len(manifest.get("gaps", []))
                retrieved_at = pd.Timestamp(manifest.get("retrieved_at"))
                if (
                    datetime.now(timezone.utc)
                    - retrieved_at.to_pydatetime().astimezone(timezone.utc)
                ).total_seconds() > 48 * 3600:
                    # Historical data remains immutable and reproducible even
                    # when it is not fresh enough for a live-like study.
                    base["freshness_status"] = "stale"
                    base["freshness_warning"] = "The managed dataset has not been refreshed in 48 hours."
                else:
                    base["freshness_status"] = "fresh"
            except (OSError, ValueError, AttributeError, TypeError):
                base["freshness_status"] = "unknown"
                base["freshness_warning"] = "The dataset freshness manifest cannot be verified."
        if definition.standard:
            span_days = _integer_value(inspected.get("span_days"), 0)
            ready = _integer_value(inspected.get("rows"), 0) >= 3650 and span_days >= 3649
            base["standard_ready"] = ready
            base["status"] = "ready" if ready else "invalid"
            if not ready:
                base["warning"] = "The Bitstamp standard requires ten complete years."
        return base

    def _describe_local(self, path: Path) -> JsonDict | None:
        inspected = _inspect_parquet(path)
        if inspected is None or _integer_value(inspected.get("rows"), 0) <= 0:
            return None
        parts = path.stem.split("_")
        contract = _local_dataset_contract(path)
        provider = str(contract.get("provider", parts[0] if parts else "local"))
        timeframe = str(contract.get("timeframe", parts[-1] if parts else "unknown"))
        symbol_token = str(contract.get("symbol", parts[1] if len(parts) > 2 else path.stem))
        market = str(contract.get("market", "spot"))
        if market not in {"spot", "perpetual"}:
            return None
        return {
            "dataset_id": path.stem.lower().replace("_", "-"),
            "key": path.stem,
            "provider": provider,
            "symbol": symbol_token,
            "timeframe": timeframe,
            "market": market,
            "feed_types": ["bar"],
            "precisions": ["bar"],
            "standard": False,
            "standard_ready": False,
            "status": "ready",
            "source": "local parquet",
            "freshness_status": "not_managed",
            **inspected,
        }

    def _local_path_for_id(self, dataset_id: str) -> Path | None:
        """Resolve an unmanaged ID by filename only; never scan parquet payloads."""

        directory = self.parquet_root / "ohlcv"
        if not directory.exists():
            return None
        for path in directory.glob("*.parquet"):
            if path.stem.lower().replace("_", "-") == dataset_id:
                return path
        return None


class DatasetSynchronizer:
    """Synchronize managed public datasets and atomically publish a manifest."""

    def __init__(
        self,
        parquet_root: Path,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.parquet_root = parquet_root
        self._client = client

    def sync(self, dataset_id: str) -> JsonDict:
        if dataset_id != "bitstamp-btcusd-1d-10y":
            raise ValueError(f"dataset does not support synchronization: {dataset_id}")
        current_year = datetime.now(timezone.utc).year
        request = DataRequest(
            provider="bitstamp",
            symbol="BTC/USD",
            timeframe="1d",
            start=datetime(current_year - 10, 1, 1, tzinfo=timezone.utc),
            end=datetime(current_year, 1, 1, tzinfo=timezone.utc),
            market="spot",
            require_real=True,
            require_complete=True,
        )
        owned_client = self._client is None
        client = self._client or httpx.Client(timeout=30)
        try:
            dataset = BitstampProvider(client).fetch(request)
        finally:
            if owned_client:
                client.close()
        if dataset.manifest.gaps:
            raise ValueError("Bitstamp standard contains missing daily candles")
        target_dir = self.parquet_root / "ohlcv"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "bitstamp_BTCUSD_1d.parquet"
        manifest_target = target.with_suffix(".manifest.json")
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=".bitstamp-", suffix=".parquet", dir=target_dir
        )
        os.close(temporary_fd)
        temporary = Path(temporary_name)
        manifest_temporary = temporary.with_suffix(".manifest.json")
        try:
            dataset.frame.to_parquet(temporary)
            manifest_temporary.write_text(
                dataset.manifest.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
            os.replace(manifest_temporary, manifest_target)
        finally:
            temporary.unlink(missing_ok=True)
            manifest_temporary.unlink(missing_ok=True)
        return DatasetCatalog(self.parquet_root).get(dataset_id)


def _local_dataset_contract(path: Path) -> JsonDict:
    """Read optional immutable local market metadata; never infer a perp from a filename."""

    manifest_path = path.with_suffix(".manifest.json")
    if not manifest_path.exists():
        return {}
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    allowed = {"provider", "symbol", "timeframe", "market"}
    return {
        key: value
        for key, value in raw.items()
        if key in allowed and isinstance(value, str) and value.strip()
    }


def _inspect_parquet(path: Path) -> JsonDict | None:
    if not path.exists():
        return None
    try:
        parquet = pq.ParquetFile(path)  # type: ignore[no-untyped-call]
        metadata = parquet.metadata
        rows = metadata.num_rows
        if rows <= 0:
            return {
                "status": "invalid",
                "rows": 0,
                "start": None,
                "end": None,
                "fingerprint": None,
                "span_days": 0,
            }
        start, end = _parquet_time_bounds(parquet)
        if start is None or end is None:
            # Statistics-free parquet is rare but valid.  Read the index only
            # as a correctness fallback, not on the normal catalog hot path.
            frame = pd.read_parquet(path, columns=["__index_level_0__"])
            if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
                return _invalid_dataset()
            start, end = frame.index.min(), frame.index.max()
    except (OSError, ValueError, pa.ArrowInvalid):
        return _invalid_dataset()
    if start is None or end is None:
        return _invalid_dataset()
    fingerprint = _immutable_file_fingerprint(path)
    return {
        "status": "ready",
        "rows": rows,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "fingerprint": fingerprint,
        "span_days": int((end - start).total_seconds() // 86400),
    }


def _invalid_dataset() -> JsonDict:
    return {
        "status": "invalid",
        "rows": 0,
        "start": None,
        "end": None,
        "fingerprint": None,
        "span_days": 0,
    }


def _parquet_time_bounds(parquet: pq.ParquetFile) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Use row-group statistics for timestamp bounds without loading candles."""

    metadata = parquet.metadata
    column_index = next(
        (
            index
            for index in range(metadata.num_columns)
            if metadata.schema.column(index).name in {"__index_level_0__", "date", "timestamp"}
        ),
        None,
    )
    if column_index is None:
        return None, None
    values: list[tuple[object, object]] = []
    for row_group in range(metadata.num_row_groups):
        statistics = metadata.row_group(row_group).column(column_index).statistics
        if statistics is None or not statistics.has_min_max:
            return None, None
        values.append((statistics.min, statistics.max))
    try:
        starts = [pd.Timestamp(value[0]) for value in values]
        ends = [pd.Timestamp(value[1]) for value in values]
    except (TypeError, ValueError):
        return None, None
    return min(starts), max(ends)


def _immutable_file_fingerprint(path: Path) -> str:
    """Cache a content hash by inode metadata; recompute only after replacement."""

    state = path.stat()
    index_path = path.with_suffix(".workbench-index.json")
    try:
        cached = json.loads(index_path.read_text(encoding="utf-8"))
        if (
            cached.get("size") == state.st_size
            and cached.get("mtime_ns") == state.st_mtime_ns
            and isinstance(cached.get("fingerprint"), str)
        ):
            return str(cached["fingerprint"])
    except (OSError, ValueError, TypeError):
        pass
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    fingerprint = digest.hexdigest()
    temporary = index_path.with_suffix(".workbench-index.tmp")
    try:
        temporary.write_text(
            json.dumps({"size": state.st_size, "mtime_ns": state.st_mtime_ns, "fingerprint": fingerprint}),
            encoding="utf-8",
        )
        os.replace(temporary, index_path)
    finally:
        temporary.unlink(missing_ok=True)
    return fingerprint


def _integer_value(value: object, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(str(value))
    except ValueError:
        return default
