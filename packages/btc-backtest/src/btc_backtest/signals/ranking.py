"""Deterministic reliability-weighted signal consensus and attribution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType

from btc_backtest.errors import ProviderError
from btc_backtest.signals.models import (
    RankedSignal,
    SignalContributor,
    SignalObservation,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class RankingConfig:
    reliability: Mapping[str, Decimal] = field(default_factory=dict)
    fallback_reliability: Decimal = Decimal("0.5")
    half_life_hours: Decimal = Decimal("24")
    min_providers: int = 1
    required_providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        copied = dict(self.reliability)
        values = (*copied.values(), self.fallback_reliability)
        if any(
            not value.is_finite() or not _ZERO <= value <= _ONE
            for value in values
        ):
            raise ValueError("ranking reliability must be between 0 and 1")
        if (
            not self.half_life_hours.is_finite()
            or self.half_life_hours <= 0
        ):
            raise ValueError("ranking half_life_hours must be positive")
        if self.min_providers < 1:
            raise ValueError("ranking min_providers must be positive")
        if len(set(self.required_providers)) != len(
            self.required_providers
        ):
            raise ValueError("ranking required providers must be unique")
        object.__setattr__(self, "reliability", MappingProxyType(copied))


class SignalAggregator:
    """Rank available observations with complete contributor attribution."""

    def __init__(self, config: RankingConfig | None = None) -> None:
        self.config = config or RankingConfig()

    def rank(
        self,
        observations: Iterable[SignalObservation],
        as_of: datetime,
    ) -> tuple[RankedSignal, ...]:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("ranking as_of must be timezone-aware")
        as_of_utc = as_of.astimezone(timezone.utc)
        unique = self._deduplicate(observations)
        groups: dict[tuple[str, str], list[SignalObservation]] = {}
        for item in unique:
            if (
                item.observed_at > as_of_utc
                or item.effective_at > as_of_utc
                or item.expires_at <= as_of_utc
            ):
                continue
            groups.setdefault((item.symbol, item.horizon), []).append(item)

        ranked: list[RankedSignal] = []
        for (symbol, horizon), items in groups.items():
            providers = {item.provider for item in items}
            if len(providers) < self.config.min_providers:
                continue
            if not set(self.config.required_providers).issubset(providers):
                continue
            result = self._rank_group(
                symbol,
                horizon,
                items,
                as_of_utc,
            )
            if result is not None:
                ranked.append(result)
        return tuple(
            sorted(
                ranked,
                key=lambda item: (
                    -abs(item.direction),
                    -item.confidence,
                    item.symbol,
                    item.horizon,
                    item.id,
                ),
            )
        )

    def _rank_group(
        self,
        symbol: str,
        horizon: str,
        items: list[SignalObservation],
        as_of: datetime,
    ) -> RankedSignal | None:
        weighted: list[tuple[SignalObservation, Decimal]] = []
        for item in sorted(
            items,
            key=lambda value: (
                value.provider,
                value.source_event_id,
                value.id,
            ),
        ):
            reliability = self.config.reliability.get(
                item.provider,
                self.config.fallback_reliability,
            )
            age_hours = Decimal(
                str((as_of - item.observed_at).total_seconds() / 3600)
            )
            decay = Decimal(
                str(
                    2
                    ** -float(
                        age_hours / self.config.half_life_hours
                    )
                )
            )
            weight = _clip_unit(
                reliability * item.confidence * decay
            )
            weighted.append((item, weight))
        total_weight = sum(
            (weight for _, weight in weighted),
            start=_ZERO,
        )
        if total_weight == 0:
            return None
        direction = _clip_signed(
            sum(
                (
                    item.direction * weight
                    for item, weight in weighted
                ),
                start=_ZERO,
            )
            / total_weight
        )
        disagreement = (
            sum(
                (
                    weight * abs(item.direction - direction)
                    for item, weight in weighted
                ),
                start=_ZERO,
            )
            / total_weight
        )
        agreement = _clip_unit(_ONE - disagreement)
        base_confidence = _clip_unit(
            total_weight / Decimal(len(weighted))
        )
        confidence = _clip_unit(base_confidence * agreement)
        contributors = tuple(
            SignalContributor(
                observation_id=item.id,
                provider=item.provider,
                source_type=item.source_type,
                direction=item.direction,
                weight=weight,
                provenance=item.provenance,
            )
            for item, weight in weighted
        )
        key_time = as_of.isoformat()
        return RankedSignal(
            id=f"consensus:{symbol}:{horizon}:{key_time}",
            symbol=symbol,
            horizon=horizon,
            direction=direction,
            confidence=confidence,
            as_of=as_of,
            contributors=contributors,
        )

    @staticmethod
    def _deduplicate(
        observations: Iterable[SignalObservation],
    ) -> tuple[SignalObservation, ...]:
        unique: dict[tuple[str, str], SignalObservation] = {}
        for item in sorted(
            observations,
            key=lambda value: (
                value.provider,
                value.source_event_id,
                value.id,
            ),
        ):
            key = (item.provider, item.source_event_id)
            existing = unique.get(key)
            if existing is None:
                unique[key] = item
                continue
            if existing.payload_sha256 != item.payload_sha256:
                raise ProviderError(
                    "conflicting duplicate signal observation"
                )
        return tuple(unique.values())


def _clip_unit(value: Decimal) -> Decimal:
    return max(_ZERO, min(_ONE, value))


def _clip_signed(value: Decimal) -> Decimal:
    return max(Decimal("-1"), min(_ONE, value))
