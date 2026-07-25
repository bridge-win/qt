"""Explicit deterministic synthetic market data for tests and demos."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import numpy as np
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


class SyntheticProvider:
    """Generate reproducible fixtures that can never masquerade as real data."""

    metadata = ProviderMetadata(
        id="synthetic",
        real_data=False,
        timeframes=("1h", "1d"),
        markets=("spot", "futures"),
    )

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def fetch(self, request: DataRequest) -> MarketDataset:
        if request.provider != self.metadata.id:
            raise ProviderError(
                f"synthetic provider cannot satisfy provider {request.provider}"
            )
        if request.require_real:
            raise ProviderError("request requires real data")

        frequency = "1h" if request.timeframe == "1h" else "1D"
        index = pd.date_range(
            start=request.start,
            end=request.end,
            freq=frequency,
            inclusive="left",
            name="timestamp",
        )
        raw_identity = json.dumps(
            {
                "seed": self.seed,
                "market": request.market,
                "symbol": request.symbol,
                "timeframe": request.timeframe,
                "start": request.start.isoformat(),
                "end": request.end.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(raw_identity).digest()
        generator = np.random.default_rng(
            self.seed ^ int.from_bytes(digest[:8], byteorder="big", signed=False)
        )
        returns = generator.normal(0.0, 0.015, len(index))
        close = 100.0 * np.exp(np.cumsum(returns))
        open_price = np.concatenate(([100.0], close[:-1]))
        spread = generator.uniform(0.001, 0.02, len(index))
        high = np.maximum(open_price, close) * (1.0 + spread)
        low = np.minimum(open_price, close) * (1.0 - spread)
        volume = generator.lognormal(mean=4.0, sigma=0.5, size=len(index))
        frame = pd.DataFrame(
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            },
            index=index,
        )
        normalized, gaps = validate_ohlcv(frame, request)
        fingerprint = frame_fingerprint(normalized)
        source = f"synthetic://seed/{self.seed}"
        segment = DataSegment(
            provider=self.metadata.id,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            start=request.start,
            end=request.end,
            real_data=False,
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
            delivered_start=request.start,
            delivered_end=request.end,
            retrieved_at=datetime.now(timezone.utc),
            real_data=False,
            raw_sha256=(hashlib.sha256(raw_identity).hexdigest(),),
            normalized_sha256=fingerprint,
            source=source,
            license_note="Generated synthetic data; not observed market history.",
            gaps=gaps,
            segments=(segment,),
        )
        return MarketDataset(frame=normalized, manifest=manifest)
