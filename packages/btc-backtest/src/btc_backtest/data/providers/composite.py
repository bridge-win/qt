"""Explicit multi-provider market data composition."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    DataSegment,
    MarketDataset,
)
from btc_backtest.data.providers.base import MarketDataProvider, ProviderMetadata
from btc_backtest.data.validation import frame_fingerprint, validate_ohlcv
from btc_backtest.errors import DataValidationError, ProviderError


class CompositeProvider:
    """Stitch explicitly compatible provider segments with overlap checks."""

    def __init__(
        self,
        providers: Iterable[MarketDataProvider],
        overlap_tolerance: float = 0.0,
    ) -> None:
        self._providers = tuple(providers)
        if not self._providers:
            raise ProviderError("composite provider requires at least one source")
        if not np.isfinite(overlap_tolerance) or overlap_tolerance < 0:
            raise ValueError("overlap_tolerance must be finite and non-negative")
        self.overlap_tolerance = overlap_tolerance

        first = self._providers[0].metadata
        timeframes = tuple(
            value
            for value in first.timeframes
            if all(value in provider.metadata.timeframes for provider in self._providers)
        )
        markets = tuple(
            value
            for value in first.markets
            if all(value in provider.metadata.markets for provider in self._providers)
        )
        restricted_symbols = [
            provider.metadata.symbols
            for provider in self._providers
            if provider.metadata.symbols
        ]
        if restricted_symbols and any(
            set(values) != set(restricted_symbols[0])
            for values in restricted_symbols[1:]
        ):
            raise DataValidationError(
                "composite providers must declare identical symbol capabilities"
            )
        if not timeframes:
            raise DataValidationError("composite providers share no timeframe")
        if not markets:
            raise DataValidationError("composite providers share no market")
        symbols = restricted_symbols[0] if restricted_symbols else ()
        self.metadata = ProviderMetadata(
            id="composite",
            real_data=all(provider.metadata.real_data for provider in self._providers),
            timeframes=timeframes,
            markets=markets,
            symbols=symbols,
        )

    def fetch(self, request: DataRequest) -> MarketDataset:
        if request.provider != self.metadata.id:
            raise ProviderError(
                f"composite provider cannot satisfy provider {request.provider}"
            )
        if request.require_real and not self.metadata.real_data:
            raise ProviderError("request requires real data")
        if request.timeframe not in self.metadata.timeframes:
            raise ProviderError(f"unsupported composite timeframe {request.timeframe}")
        if request.market not in self.metadata.markets:
            raise ProviderError(f"unsupported composite market {request.market}")
        if self.metadata.symbols and request.symbol not in self.metadata.symbols:
            raise DataValidationError(
                f"composite providers do not support symbol {request.symbol}"
            )

        datasets: list[MarketDataset] = []
        for provider in self._providers:
            child_request = request.model_copy(
                update={
                    "provider": provider.metadata.id,
                    "require_complete": False,
                    "max_missing_ratio": 1.0,
                }
            )
            dataset = provider.fetch(child_request)
            self._validate_child_identity(
                child_request,
                dataset,
                provider.metadata,
            )
            normalized_child, child_gaps = validate_ohlcv(
                dataset.frame,
                child_request,
            )
            if child_gaps != dataset.manifest.gaps:
                raise DataValidationError(
                    f"composite child {provider.metadata.id} gap metadata mismatch"
                )
            if (
                frame_fingerprint(normalized_child)
                != dataset.manifest.normalized_sha256
            ):
                raise DataValidationError(
                    f"composite child {provider.metadata.id} fingerprint mismatch"
                )
            dataset = MarketDataset(
                frame=normalized_child,
                manifest=dataset.manifest,
            )
            self._check_overlaps(datasets, dataset)
            datasets.append(dataset)

        combined = pd.concat([dataset.frame for dataset in datasets])
        combined = combined.loc[~combined.index.duplicated(keep="first")].sort_index()
        normalized, gaps = validate_ohlcv(combined, request)
        fingerprint = frame_fingerprint(normalized)
        segments = tuple(
            DataSegment(
                provider=dataset.manifest.provider,
                market=dataset.manifest.market,
                symbol=dataset.manifest.symbol,
                timeframe=dataset.manifest.timeframe,
                start=dataset.manifest.delivered_start,
                end=dataset.manifest.delivered_end,
                real_data=dataset.manifest.real_data,
                normalized_sha256=dataset.manifest.normalized_sha256,
                source=dataset.manifest.source,
            )
            for dataset in datasets
        )
        delta = (
            timedelta(hours=1)
            if request.timeframe == "1h"
            else timedelta(days=1)
        )
        manifest = DataManifest(
            provider=self.metadata.id,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            requested_start=request.start,
            requested_end=request.end,
            delivered_start=normalized.index[0].to_pydatetime(),
            delivered_end=(normalized.index[-1] + delta).to_pydatetime(),
            retrieved_at=datetime.now(timezone.utc),
            real_data=self.metadata.real_data,
            raw_sha256=tuple(
                digest
                for dataset in datasets
                for digest in dataset.manifest.raw_sha256
            ),
            normalized_sha256=fingerprint,
            source="composite://" + "+".join(segment.provider for segment in segments),
            license_note="See source segment manifests for licensing and provenance.",
            gaps=gaps,
            segments=segments,
        )
        return MarketDataset(frame=normalized, manifest=manifest)

    def _check_overlaps(
        self,
        previous: list[MarketDataset],
        candidate: MarketDataset,
    ) -> None:
        for existing in previous:
            overlap = existing.frame.index.intersection(candidate.frame.index)
            if overlap.empty:
                continue
            left = existing.frame.loc[overlap, ["open", "high", "low", "close", "volume"]]
            right = candidate.frame.loc[
                overlap,
                ["open", "high", "low", "close", "volume"],
            ]
            if not np.allclose(
                left.to_numpy(dtype="float64"),
                right.to_numpy(dtype="float64"),
                rtol=self.overlap_tolerance,
                atol=self.overlap_tolerance,
            ):
                raise DataValidationError(
                    f"provider overlap conflicts at {overlap[0].isoformat()}"
                )

    @staticmethod
    def _validate_child_identity(
        request: DataRequest,
        dataset: MarketDataset,
        metadata: ProviderMetadata,
    ) -> None:
        manifest = dataset.manifest
        identity = (
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
        if identity != expected:
            raise DataValidationError("composite child dataset identity mismatch")
        if manifest.real_data != metadata.real_data:
            raise DataValidationError(
                f"composite child {metadata.id} real-data label contradicts metadata"
            )
        if request.require_real and not manifest.real_data:
            raise DataValidationError(
                f"composite child {metadata.id} did not provide real data"
            )
