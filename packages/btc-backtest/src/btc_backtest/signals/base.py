"""Signal provider protocol and validating registry."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from btc_backtest.errors import ProviderError
from btc_backtest.signals.models import (
    SignalObservation,
    SignalProviderMetadata,
    SignalQuery,
)


@runtime_checkable
class SignalProvider(Protocol):
    metadata: SignalProviderMetadata

    def fetch(self, query: SignalQuery) -> tuple[SignalObservation, ...]: ...


class SignalProviderRegistry:
    """Resolve providers and validate every normalized observation."""

    def __init__(self, providers: Iterable[SignalProvider] = ()) -> None:
        registered: dict[str, SignalProvider] = {}
        for provider in providers:
            if not isinstance(provider, SignalProvider):
                raise ValueError("signal provider does not satisfy the protocol")
            provider_id = provider.metadata.id
            if provider_id in registered:
                raise ValueError(f"duplicate signal provider: {provider_id}")
            registered[provider_id] = provider
        self._providers = MappingProxyType(registered)

    def fetch(
        self,
        provider_id: str,
        query: SignalQuery,
    ) -> tuple[SignalObservation, ...]:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ProviderError(f"unknown signal provider: {provider_id}")
        if query.require_historical and not provider.metadata.historical:
            raise ProviderError(
                f"signal provider {provider_id} has no historical capability"
            )
        try:
            observations = provider.fetch(query)
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                f"signal provider {provider_id} fetch failed"
            ) from error
        if not isinstance(observations, tuple):
            raise ProviderError(
                f"signal provider {provider_id} must return a tuple"
            )
        for observation in observations:
            _validate_observation(provider, query, observation)
        return observations

    def list(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    @property
    def providers(self) -> Mapping[str, SignalProvider]:
        return self._providers


def _validate_observation(
    provider: SignalProvider,
    query: SignalQuery,
    observation: SignalObservation,
) -> None:
    if not isinstance(observation, SignalObservation):
        raise ProviderError(
            f"signal provider {provider.metadata.id} returned an invalid item"
        )
    if observation.provider != provider.metadata.id:
        raise ProviderError(
            f"signal observation provider {observation.provider} does not "
            f"match {provider.metadata.id}"
        )
    if observation.source_type not in provider.metadata.source_types:
        raise ProviderError(
            f"signal provider {provider.metadata.id} returned undeclared "
            f"source type {observation.source_type}"
        )
    if observation.symbol != query.symbol:
        raise ProviderError("signal observation symbol does not match query")
    if observation.horizon not in query.horizons:
        raise ProviderError("signal observation horizon does not match query")
    if (
        observation.effective_at < query.start
        or observation.effective_at >= query.end
    ):
        raise ProviderError("signal observation is outside query interval")
    if (
        query.source_types
        and observation.source_type not in query.source_types
    ):
        raise ProviderError("signal observation source type is outside query")
