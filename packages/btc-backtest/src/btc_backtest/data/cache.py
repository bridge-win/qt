"""Atomic, content-addressed Parquet cache for validated market data."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from btc_backtest.data.models import (
    SHA256_PATTERN,
    DataManifest,
    DataRequest,
    MarketDataset,
)
from btc_backtest.data.validation import frame_fingerprint, validate_ohlcv
from btc_backtest.errors import DataValidationError


class _CachePointer(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = "1"
    version: str = Field(pattern=SHA256_PATTERN)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _request_key(request: DataRequest) -> str:
    payload = {
        "schema_version": "1",
        "provider": request.provider,
        "market": request.market,
        "symbol": request.symbol,
        "timeframe": request.timeframe,
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
    }
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _manifest_version(manifest: DataManifest) -> str:
    return hashlib.sha256(
        _json_bytes(manifest.model_dump(mode="json"))
    ).hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DataCache:
    """Store immutable data versions and atomically publish request pointers."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._versions = root / "versions"
        self._requests = root / "requests"
        self._versions.mkdir(parents=True, exist_ok=True)
        self._requests.mkdir(parents=True, exist_ok=True)

    def load(self, request: DataRequest) -> MarketDataset | None:
        """Load and revalidate the currently published entry for a request."""

        pointer_path = self._pointer_path(request)
        if not pointer_path.exists():
            return None

        try:
            pointer = _CachePointer.model_validate_json(pointer_path.read_text())
            version_directory = self._versions / pointer.version
            manifest_path = version_directory / "manifest.json"
            data_path = version_directory / "data.parquet"
            manifest = DataManifest.model_validate_json(manifest_path.read_text())
            if _manifest_version(manifest) != pointer.version:
                raise DataValidationError("cached manifest version does not match pointer")
            frame = pd.read_parquet(data_path)
            normalized, gaps = validate_ohlcv(frame, request)
            self._validate_identity(request, manifest)
            if gaps != manifest.gaps:
                raise DataValidationError("cached gap metadata does not match market data")
            if frame_fingerprint(normalized) != manifest.normalized_sha256:
                raise DataValidationError("cached data fingerprint does not match manifest")
        except DataValidationError:
            raise
        except (OSError, ValueError, ValidationError) as error:
            raise DataValidationError(
                f"cached entry for request {_request_key(request)} is corrupt"
            ) from error

        return MarketDataset(frame=normalized, manifest=manifest)

    def publish(self, request: DataRequest, dataset: MarketDataset) -> Path:
        """Validate and atomically publish a dataset for a request."""

        normalized, gaps = validate_ohlcv(dataset.frame, request)
        self._validate_identity(request, dataset.manifest)
        if gaps != dataset.manifest.gaps:
            raise DataValidationError("dataset gap metadata does not match market data")
        fingerprint = frame_fingerprint(normalized)
        if fingerprint != dataset.manifest.normalized_sha256:
            raise DataValidationError("dataset fingerprint does not match manifest")

        version = _manifest_version(dataset.manifest)
        target_directory = self._versions / version
        with TemporaryDirectory(dir=self.root, prefix=".publish-") as temporary:
            temporary_root = Path(temporary)
            candidate_directory = temporary_root / version
            candidate_directory.mkdir()
            data_path = candidate_directory / "data.parquet"
            manifest_path = candidate_directory / "manifest.json"
            normalized.to_parquet(data_path)
            manifest_path.write_text(
                dataset.manifest.model_dump_json(indent=2),
                encoding="utf-8",
            )
            _fsync_file(data_path)
            _fsync_file(manifest_path)
            _fsync_directory(candidate_directory)

            if not target_directory.exists():
                try:
                    candidate_directory.replace(target_directory)
                except OSError:
                    if not target_directory.is_dir():
                        raise
                _fsync_directory(self._versions)

            pointer_candidate = temporary_root / "pointer.json"
            pointer_candidate.write_text(
                _CachePointer(version=version).model_dump_json(),
                encoding="utf-8",
            )
            _fsync_file(pointer_candidate)
            pointer_candidate.replace(self._pointer_path(request))
            _fsync_directory(self._requests)

        return target_directory / "data.parquet"

    def _pointer_path(self, request: DataRequest) -> Path:
        return self._requests / f"{_request_key(request)}.json"

    @staticmethod
    def _validate_identity(request: DataRequest, manifest: DataManifest) -> None:
        actual = (
            manifest.provider,
            manifest.market,
            manifest.symbol,
            manifest.timeframe,
            manifest.requested_start,
            manifest.requested_end,
        )
        expected = (
            request.provider,
            request.market,
            request.symbol,
            request.timeframe,
            request.start,
            request.end,
        )
        if actual != expected:
            raise DataValidationError("dataset identity does not match request")
        if request.require_real and not manifest.real_data:
            raise DataValidationError("request requires real data")
