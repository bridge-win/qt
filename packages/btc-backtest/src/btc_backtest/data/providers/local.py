"""Immutable local Parquet market data provider."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    DataSegment,
    MarketDataset,
)
from btc_backtest.data.providers.base import ProviderMetadata
from btc_backtest.data.validation import frame_fingerprint, validate_ohlcv
from btc_backtest.errors import ProviderError


class LocalParquetProvider:
    """Read an immutable user-owned Parquet file through normal validation."""

    metadata = ProviderMetadata(
        id="local",
        real_data=True,
        timeframes=("1h", "1d"),
        markets=("spot", "futures"),
    )

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def fetch(self, request: DataRequest) -> MarketDataset:
        if request.provider != self.metadata.id:
            raise ProviderError(
                f"local provider cannot satisfy provider {request.provider}"
            )
        try:
            raw = self.path.read_bytes()
            frame = pd.read_parquet(self.path)
        except (OSError, ValueError) as error:
            raise ProviderError(f"failed to read local Parquet file {self.path}") from error

        normalized, gaps = validate_ohlcv(frame, request)
        delta = _timeframe_delta(request)
        delivered_start = normalized.index[0].to_pydatetime()
        delivered_end = (normalized.index[-1] + delta).to_pydatetime()
        fingerprint = frame_fingerprint(normalized)
        source = str(self.path)
        segment = DataSegment(
            provider=self.metadata.id,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            start=delivered_start,
            end=delivered_end,
            real_data=True,
            normalized_sha256=fingerprint,
            source=source,
        )
        manifest = DataManifest(
            provider=self.metadata.id,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            requested_start=request.start,
            requested_end=request.end,
            delivered_start=delivered_start,
            delivered_end=delivered_end,
            retrieved_at=datetime.now(timezone.utc),
            real_data=True,
            raw_sha256=(hashlib.sha256(raw).hexdigest(),),
            normalized_sha256=fingerprint,
            source=source,
            license_note="User-provided local data; licensing is the user's responsibility.",
            gaps=gaps,
            segments=(segment,),
        )
        return MarketDataset(frame=normalized, manifest=manifest)


def _timeframe_delta(request: DataRequest) -> timedelta:
    if request.timeframe == "1h":
        return timedelta(hours=1)
    return timedelta(days=1)
