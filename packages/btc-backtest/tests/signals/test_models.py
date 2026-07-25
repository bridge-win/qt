from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from btc_backtest.signals.models import (
    RankedSignal,
    SignalContributor,
    SignalObservation,
    SignalProviderMetadata,
    SignalQuery,
)
from pydantic import ValidationError

UTC = timezone.utc


def _time(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _observation(**updates: object) -> SignalObservation:
    values: dict[str, object] = {
        "id": "fixture:1",
        "source_event_id": "1",
        "provider": "fixture",
        "source_type": "sentiment",
        "symbol": "BTC/USD",
        "horizon": "1d",
        "direction": Decimal("0.8"),
        "confidence": Decimal("0.5"),
        "observed_at": _time(2),
        "effective_at": _time(1),
        "expires_at": _time(3),
        "provenance": "fixture://event/1",
        "payload_sha256": "0" * 64,
        "quality_flags": ("historical",),
    }
    values.update(updates)
    return SignalObservation.model_validate(values)


@pytest.mark.parametrize(
    "updates",
    [
        {"direction": Decimal("1.1")},
        {"confidence": Decimal("-0.1")},
        {"observed_at": datetime(2024, 1, 2)},
        {"effective_at": _time(4)},
        {"observed_at": _time(4)},
        {"provider": "Fixture"},
        {"payload_sha256": "A" * 64},
        {"payload_sha256": "0" * 63},
        {"provenance": ""},
        {"quality_flags": ("same", "same")},
    ],
)
def test_observation_requires_point_in_time_bounds(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _observation(**updates)


def test_observation_is_frozen_and_normalizes_to_utc() -> None:
    east = timezone(timedelta(hours=8))
    observation = _observation(observed_at=_time(2).astimezone(east))

    assert observation.observed_at.tzinfo is UTC
    with pytest.raises(ValidationError):
        observation.direction = Decimal("0")


def test_query_is_closed_open_and_deduplicates_filters() -> None:
    query = SignalQuery(
        start=_time(1),
        end=_time(3),
        symbol="BTC/USD",
        horizons=("1d", "1h"),
        source_types=("sentiment", "funding"),
    )

    assert query.horizons == ("1d", "1h")
    with pytest.raises(ValidationError):
        SignalQuery(
            start=_time(3),
            end=_time(1),
            symbol="BTC/USD",
            horizons=("1d",),
        )
    with pytest.raises(ValidationError):
        SignalQuery.model_validate(
            {**query.model_dump(), "horizons": ("1d", "1d")}
        )


def test_ranked_signal_preserves_bounded_attribution() -> None:
    contributor = SignalContributor(
        observation_id="fixture:1",
        provider="fixture",
        source_type="sentiment",
        direction=Decimal("0.8"),
        weight=Decimal("0.4"),
        provenance="fixture://event/1",
    )

    ranked = RankedSignal(
        id="ranked:1",
        symbol="BTC/USD",
        horizon="1d",
        direction=Decimal("0.8"),
        confidence=Decimal("0.4"),
        as_of=_time(2),
        contributors=(contributor,),
    )

    assert ranked.contributors == (contributor,)
    with pytest.raises(ValidationError):
        RankedSignal.model_validate(
            {**ranked.model_dump(), "confidence": Decimal("2")}
        )


def test_provider_metadata_declares_unique_source_types() -> None:
    metadata = SignalProviderMetadata(
        id="fixture",
        description="Fixture signals.",
        source_types=("sentiment", "funding"),
        historical=True,
    )

    assert metadata.source_types == ("sentiment", "funding")
    with pytest.raises(ValidationError):
        SignalProviderMetadata.model_validate(
            {
                **metadata.model_dump(),
                "source_types": ("sentiment", "sentiment"),
            }
        )
