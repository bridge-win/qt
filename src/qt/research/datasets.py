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
        for dataset in self.list_datasets():
            if dataset["dataset_id"] == dataset_id:
                return dataset
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
            "standard": definition.standard,
            "standard_ready": False,
            "status": "missing",
            "rows": 0,
            "start": None,
            "end": None,
            "fingerprint": None,
            "gaps": None,
            "retrieved_at": None,
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
                    base["status"] = "stale"
                    base["standard_ready"] = False
                    base["warning"] = (
                        "The managed dataset has not been refreshed in 48 hours."
                    )
                    return base
            except (OSError, ValueError, AttributeError, TypeError):
                base["status"] = "invalid"
                base["warning"] = "The dataset manifest cannot be verified."
                return base
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
        provider = parts[0] if parts else "local"
        timeframe = parts[-1] if parts else "unknown"
        symbol_token = parts[1] if len(parts) > 2 else path.stem
        return {
            "dataset_id": path.stem.lower().replace("_", "-"),
            "key": path.stem,
            "provider": provider,
            "symbol": symbol_token,
            "timeframe": timeframe,
            "standard": False,
            "standard_ready": False,
            "status": "ready",
            "source": "local parquet",
            **inspected,
        }


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


def _inspect_parquet(path: Path) -> JsonDict | None:
    if not path.exists():
        return None
    try:
        frame = pd.read_parquet(path)
    except (OSError, ValueError):
        return {
            "status": "invalid",
            "rows": 0,
            "start": None,
            "end": None,
            "fingerprint": None,
            "span_days": 0,
        }
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return {
            "status": "invalid",
            "rows": 0,
            "start": None,
            "end": None,
            "fingerprint": None,
            "span_days": 0,
        }
    start = frame.index.min()
    end = frame.index.max()
    fingerprint = hashlib.sha256(
        frame.to_csv(index=True, float_format="%.12g").encode("utf-8")
    ).hexdigest()
    return {
        "status": "ready",
        "rows": len(frame),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "fingerprint": fingerprint,
        "span_days": int((end - start).total_seconds() // 86400),
    }


def _integer_value(value: object, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(str(value))
    except ValueError:
        return default
