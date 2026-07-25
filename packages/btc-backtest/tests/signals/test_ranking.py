from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from btc_backtest.errors import ProviderError
from btc_backtest.signals.models import SignalObservation
from btc_backtest.signals.ranking import RankingConfig, SignalAggregator

from .helpers import observation

UTC = timezone.utc
NOW = datetime(2024, 1, 3, tzinfo=UTC)


def signal(
    provider: str,
    *,
    direction: str,
    confidence: str,
    age_hours: int = 0,
    source_event_id: str | None = None,
    horizon: str = "1d",
) -> SignalObservation:
    observed_at = NOW - timedelta(hours=age_hours)
    return observation(
        id=f"{provider}:{source_event_id or provider}",
        source_event_id=source_event_id or provider,
        provider=provider,
        direction=Decimal(direction),
        confidence=Decimal(confidence),
        effective_at=observed_at,
        observed_at=observed_at,
        expires_at=NOW + timedelta(days=1),
        horizon=horizon,
        provenance=f"https://{provider}.example/event",
    )


def test_consensus_applies_reliability_confidence_and_decay() -> None:
    observations = (
        signal("a", direction="1", confidence="0.8"),
        signal(
            "b",
            direction="-1",
            confidence="0.5",
            age_hours=24,
        ),
    )
    ranked = SignalAggregator(
        RankingConfig(
            reliability={
                "a": Decimal("0.75"),
                "b": Decimal("0.50"),
            },
            half_life_hours=Decimal("24"),
        )
    ).rank(observations, as_of=NOW)
    expected = (
        Decimal("0.8") * Decimal("0.75")
        - Decimal("0.5") * Decimal("0.50") * Decimal("0.5")
    ) / (
        Decimal("0.8") * Decimal("0.75")
        + Decimal("0.5") * Decimal("0.50") * Decimal("0.5")
    )

    assert ranked[0].direction == pytest.approx(expected)
    assert {
        item.provider for item in ranked[0].contributors
    } == {"a", "b"}
    assert ranked[0].confidence < Decimal("1")


def test_rank_rejects_future_known_and_expired_observations() -> None:
    future = signal("future", direction="1", confidence="1").model_copy(
        update={"observed_at": NOW + timedelta(seconds=1)}
    )
    expired = signal("expired", direction="1", confidence="1").model_copy(
        update={"expires_at": NOW}
    )

    assert SignalAggregator().rank((future, expired), as_of=NOW) == ()


def test_rank_groups_and_orders_deterministically() -> None:
    observations = (
        signal("weak", direction="0.2", confidence="1", horizon="8h"),
        signal("strong", direction="-0.9", confidence="1"),
    )

    ranked = SignalAggregator().rank(observations, as_of=NOW)

    assert [item.horizon for item in ranked] == ["1d", "8h"]
    assert ranked == SignalAggregator().rank(
        tuple(reversed(observations)),
        as_of=NOW,
    )


def test_rank_enforces_minimum_and_required_providers() -> None:
    observations = (
        signal("a", direction="1", confidence="1"),
        signal("b", direction="1", confidence="1"),
    )

    assert SignalAggregator(
        RankingConfig(min_providers=3)
    ).rank(observations, as_of=NOW) == ()
    assert SignalAggregator(
        RankingConfig(required_providers=("a", "c"))
    ).rank(observations, as_of=NOW) == ()


def test_rank_deduplicates_replay_and_rejects_conflict() -> None:
    first = signal(
        "a",
        direction="1",
        confidence="1",
        source_event_id="same",
    )
    replay = first.model_copy(update={"id": "other-id"})
    conflict = first.model_copy(
        update={"payload_sha256": "1" * 64}
    )

    ranked = SignalAggregator().rank((first, replay), as_of=NOW)
    assert len(ranked[0].contributors) == 1
    with pytest.raises(ProviderError, match="conflicting"):
        SignalAggregator().rank((first, conflict), as_of=NOW)


def test_equal_opposition_has_zero_confidence() -> None:
    ranked = SignalAggregator().rank(
        (
            signal("a", direction="1", confidence="1"),
            signal("b", direction="-1", confidence="1"),
        ),
        as_of=NOW,
    )[0]

    assert ranked.direction == 0
    assert ranked.confidence == 0
