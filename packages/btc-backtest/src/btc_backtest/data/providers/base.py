"""Market data provider protocol, capabilities, and registry."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from btc_backtest.data.cache import DataCache
from btc_backtest.data.models import DataRequest, MarketDataset, Timeframe
from btc_backtest.errors import ProviderError


class ProviderMetadata(BaseModel):
    """Static capabilities declared by a market data provider."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    real_data: bool
    timeframes: tuple[Timeframe, ...] = Field(min_length=1)
    markets: tuple[str, ...] = Field(default=("spot",), min_length=1)
    symbols: tuple[str, ...] = ()

    @field_validator("timeframes", "markets", "symbols")
    @classmethod
    def require_unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("provider capabilities must be unique")
        if any(not value.strip() for value in values):
            raise ValueError("provider capabilities must not be blank")
        return values


class MarketDataProvider(Protocol):
    """Structural contract implemented by all provider adapters."""

    @property
    def metadata(self) -> ProviderMetadata: ...

    def fetch(self, request: DataRequest) -> MarketDataset: ...


class ProviderRegistry:
    """Resolve provider capabilities, cache hits, and validated fetches."""

    def __init__(self, providers: Iterable[MarketDataProvider]) -> None:
        by_id: dict[str, MarketDataProvider] = {}
        for provider in providers:
            provider_id = provider.metadata.id
            if provider_id in by_id:
                raise ProviderError(f"duplicate provider: {provider_id}")
            by_id[provider_id] = provider
        self._providers = MappingProxyType(by_id)

    @property
    def providers(self) -> Mapping[str, MarketDataProvider]:
        return self._providers

    def fetch(self, request: DataRequest, cache: DataCache) -> MarketDataset:
        provider = self._providers.get(request.provider)
        if provider is None:
            raise ProviderError(f"unknown provider: {request.provider}")
        metadata = provider.metadata
        if request.timeframe not in metadata.timeframes:
            raise ProviderError(
                f"provider {metadata.id} does not support timeframe {request.timeframe}"
            )
        if request.market not in metadata.markets:
            raise ProviderError(
                f"provider {metadata.id} does not support market {request.market}"
            )
        if metadata.symbols and request.symbol not in metadata.symbols:
            raise ProviderError(
                f"provider {metadata.id} does not support symbol {request.symbol}"
            )
        if request.require_real and not metadata.real_data:
            raise ProviderError(
                f"request requires real data but provider {metadata.id} is synthetic"
            )

        cached = cache.load(request)
        if cached is not None:
            return cached

        dataset = provider.fetch(request)
        cache.publish(request, dataset)
        published = cache.load(request)
        if published is None:
            raise ProviderError(f"provider {metadata.id} cache publication was not visible")
        return published
