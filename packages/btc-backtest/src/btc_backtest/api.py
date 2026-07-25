"""Public orchestration API for data-backed strategy execution."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import cast

from btc_backtest.data.cache import DataCache
from btc_backtest.data.models import DataRequest, MarketBundle
from btc_backtest.data.providers.base import (
    MarketDataProvider,
    ProviderRegistry,
)
from btc_backtest.engine.models import BacktestResult, BacktestSpec
from btc_backtest.engine.runner import EventRunner
from btc_backtest.errors import ProviderError, StrategyLoadError
from btc_backtest.strategies.base import Strategy

ConfiguredStrategyFactory = Callable[[Mapping[str, object]], Strategy]
StrategyRegistration = (
    Strategy | type[Strategy] | ConfiguredStrategyFactory
)


class BacktestRunner:
    """Resolve providers and strategies, then delegate one immutable run."""

    def __init__(
        self,
        provider_registry: Mapping[str, MarketDataProvider] | None = None,
        strategy_registry: Mapping[str, StrategyRegistration] | None = None,
        cache: DataCache | None = None,
        engine: EventRunner | None = None,
    ) -> None:
        providers = dict(provider_registry or {})
        for provider_id, provider in providers.items():
            if provider_id != provider.metadata.id:
                raise ProviderError(
                    f"provider registry key {provider_id} does not match "
                    f"metadata id {provider.metadata.id}"
                )
        self._providers = ProviderRegistry(providers.values())
        self._strategies = MappingProxyType(dict(strategy_registry or {}))
        self._cache = cache or DataCache(Path(".btc-backtest-cache"))
        self._engine = engine or EventRunner()

    def run(
        self,
        spec: BacktestSpec,
        strategy: Strategy | None = None,
    ) -> BacktestResult:
        """Fetch all declared datasets and execute exactly one engine run."""

        active_strategy = strategy or self._resolve_strategy(
            spec.strategy,
            spec.strategy_params,
        )
        if not isinstance(active_strategy, Strategy):
            raise StrategyLoadError(
                f"strategy {spec.strategy} does not satisfy the Strategy protocol"
            )
        primary = self._providers.fetch(spec.data, self._cache)
        auxiliary = {}
        for index, request in enumerate(spec.auxiliary_data, start=1):
            name = _auxiliary_name(request, index)
            if name in auxiliary:
                raise ProviderError(f"duplicate auxiliary dataset name: {name}")
            auxiliary[name] = self._providers.fetch(request, self._cache)
        return self._engine.run(
            spec,
            MarketBundle(primary=primary, auxiliary=auxiliary),
            active_strategy,
        )

    def _resolve_strategy(
        self,
        strategy_id: str,
        parameters: Mapping[str, object],
    ) -> Strategy:
        registration = self._strategies.get(strategy_id)
        if registration is None:
            raise StrategyLoadError(f"unknown strategy: {strategy_id}")
        try:
            if inspect.isclass(registration):
                candidate = registration()
            elif isinstance(registration, Strategy):
                candidate = registration
            else:
                candidate = cast(
                    ConfiguredStrategyFactory,
                    registration,
                )(parameters)
        except Exception as error:
            raise StrategyLoadError(
                f"failed to construct strategy: {strategy_id}"
            ) from error
        if not isinstance(candidate, Strategy):
            raise StrategyLoadError(
                f"strategy {strategy_id} does not satisfy the Strategy protocol"
            )
        return candidate


def _auxiliary_name(request: DataRequest, index: int) -> str:
    if request.market:
        return request.market
    return f"auxiliary-{index}"
