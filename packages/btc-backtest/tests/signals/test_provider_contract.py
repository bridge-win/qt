from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from btc_backtest.errors import ProviderError
from btc_backtest.signals.base import (
    SignalProvider,
    SignalProviderRegistry,
)
from btc_backtest.signals.models import (
    SignalObservation,
    SignalProviderMetadata,
    SignalQuery,
)

UTC = timezone.utc


def _time(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _query(*, require_historical: bool = True) -> SignalQuery:
    return SignalQuery(
        start=_time(1),
        end=_time(4),
        symbol="BTC/USD",
        horizons=("1d",),
        require_historical=require_historical,
    )


class FixtureProvider:
    metadata = SignalProviderMetadata(
        id="fixture",
        description="Fixture provider.",
        source_types=("sentiment",),
        historical=True,
    )

    def fetch(self, query: SignalQuery) -> tuple[SignalObservation, ...]:
        return (
            SignalObservation(
                id="fixture:1",
                source_event_id="1",
                provider="fixture",
                source_type="sentiment",
                symbol=query.symbol,
                horizon="1d",
                direction=Decimal("0.5"),
                confidence=Decimal("0.75"),
                observed_at=_time(2),
                effective_at=_time(1),
                expires_at=_time(3),
                provenance="fixture://1",
                payload_sha256="0" * 64,
            ),
        )


class LiveProvider(FixtureProvider):
    metadata = FixtureProvider.metadata.model_copy(
        update={"id": "live", "historical": False},
    )

    def fetch(self, query: SignalQuery) -> tuple[SignalObservation, ...]:
        item = super().fetch(query)[0]
        return (item.model_copy(update={"provider": "live"}),)


def test_signal_provider_protocol_is_runtime_checkable() -> None:
    assert isinstance(FixtureProvider(), SignalProvider)


def test_registry_rejects_duplicate_provider_ids() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        SignalProviderRegistry([FixtureProvider(), FixtureProvider()])


def test_registry_fetches_and_validates_provider_identity() -> None:
    registry = SignalProviderRegistry([FixtureProvider()])

    observations = registry.fetch("fixture", _query())

    assert observations[0].provider == "fixture"
    assert registry.list() == ("fixture",)


def test_registry_rejects_unknown_and_live_only_historical_provider() -> None:
    registry = SignalProviderRegistry([FixtureProvider(), LiveProvider()])

    with pytest.raises(ProviderError, match="unknown"):
        registry.fetch("missing", _query())
    with pytest.raises(ProviderError, match="historical"):
        registry.fetch("live", _query())


def test_registry_mappings_are_immutable() -> None:
    registry = SignalProviderRegistry([FixtureProvider()])

    with pytest.raises(TypeError):
        registry.providers["other"] = FixtureProvider()  # type: ignore[index]
