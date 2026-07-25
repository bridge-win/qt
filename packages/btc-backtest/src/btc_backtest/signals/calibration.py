"""Out-of-sample signal provider reliability calibration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from btc_backtest.data.models import SHA256_PATTERN
from btc_backtest.signals.models import IDENTIFIER_PATTERN

_ZERO = Decimal("0")
_ONE = Decimal("1")
_MIN_FALLBACK = Decimal("0.1")
_MAX_FALLBACK = Decimal("0.9")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("calibration timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


class ProviderOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    provider: str = Field(pattern=IDENTIFIER_PATTERN)
    observation_id: str = Field(min_length=1)
    horizon_closes_at: datetime
    correct: bool
    payload_sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("horizon_closes_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class CalibrationWindow(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    start: datetime
    end: datetime
    outcomes: tuple[ProviderOutcome, ...] = ()
    source_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("start", "end")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_interval(self) -> CalibrationWindow:
        if self.end <= self.start:
            raise ValueError("calibration window end must be after start")
        return self


class ReliabilitySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    provider: str = Field(pattern=IDENTIFIER_PATTERN)
    alpha: Decimal = Field(gt=0)
    beta: Decimal = Field(gt=0)
    through: datetime
    sample_count: int = Field(ge=0)
    source_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("through")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("alpha", "beta")
    @classmethod
    def require_finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("reliability parameters must be finite")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reliability(self) -> Decimal:
        return self.alpha / (self.alpha + self.beta)


class ReliabilityCalibrator:
    """Create immutable reliability snapshots without evaluation leakage."""

    def __init__(
        self,
        *,
        alpha0: Decimal = Decimal("2"),
        beta0: Decimal = Decimal("2"),
        fallback_prior: Decimal = Decimal("0.5"),
    ) -> None:
        for name, value in {
            "alpha0": alpha0,
            "beta0": beta0,
        }.items():
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not fallback_prior.is_finite():
            raise ValueError("fallback_prior must be finite")
        self.alpha0 = alpha0
        self.beta0 = beta0
        self.fallback_prior = _clamp(
            fallback_prior,
            minimum=_MIN_FALLBACK,
            maximum=_MAX_FALLBACK,
        )

    def initial_snapshot(
        self,
        *,
        provider: str,
        through: datetime,
        source_fingerprint: str = "0" * 64,
    ) -> ReliabilitySnapshot:
        return ReliabilitySnapshot(
            provider=provider,
            alpha=self.alpha0,
            beta=self.beta0,
            through=through,
            sample_count=0,
            source_fingerprint=source_fingerprint,
        )

    def update(
        self,
        previous: ReliabilitySnapshot,
        completed_window: CalibrationWindow,
    ) -> ReliabilitySnapshot:
        if previous.through >= completed_window.start:
            raise ValueError(
                "calibration snapshot must be completed before window start"
            )
        provider_outcomes = tuple(
            outcome
            for outcome in completed_window.outcomes
            if outcome.provider == previous.provider
        )
        open_outcomes = tuple(
            outcome
            for outcome in provider_outcomes
            if outcome.horizon_closes_at > completed_window.end
        )
        if open_outcomes:
            raise ValueError(
                "calibration outcome horizon closes after completed window"
            )
        wins = sum(1 for outcome in provider_outcomes if outcome.correct)
        losses = len(provider_outcomes) - wins
        fingerprint = _fingerprint_snapshot(
            previous=previous,
            window=completed_window,
            outcomes=provider_outcomes,
        )
        return ReliabilitySnapshot(
            provider=previous.provider,
            alpha=previous.alpha + Decimal(wins),
            beta=previous.beta + Decimal(losses),
            through=completed_window.end,
            sample_count=previous.sample_count + len(provider_outcomes),
            source_fingerprint=fingerprint,
        )

    def weights_for(
        self,
        *,
        snapshots: tuple[ReliabilitySnapshot, ...],
        window_start: datetime,
        providers: tuple[str, ...] = (),
    ) -> Mapping[str, Decimal]:
        start = _utc(window_start)
        weights: dict[str, Decimal] = {}
        for snapshot in snapshots:
            if snapshot.through >= start:
                raise ValueError(
                    "calibration snapshot must be completed before "
                    "evaluation window start"
                )
            weights[snapshot.provider] = snapshot.reliability
        for provider in providers:
            weights.setdefault(provider, self.fallback_prior)
        return MappingProxyType(weights)


def _fingerprint_snapshot(
    *,
    previous: ReliabilitySnapshot,
    window: CalibrationWindow,
    outcomes: tuple[ProviderOutcome, ...],
) -> str:
    payload = {
        "previous": {
            "provider": previous.provider,
            "alpha": str(previous.alpha),
            "beta": str(previous.beta),
            "through": previous.through.isoformat(),
            "sample_count": previous.sample_count,
            "source_fingerprint": previous.source_fingerprint,
        },
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "source_fingerprint": window.source_fingerprint,
        },
        "outcomes": [
            {
                "provider": item.provider,
                "observation_id": item.observation_id,
                "horizon_closes_at": item.horizon_closes_at.isoformat(),
                "correct": item.correct,
                "payload_sha256": item.payload_sha256,
            }
            for item in sorted(
                outcomes,
                key=lambda item: (
                    item.provider,
                    item.observation_id,
                    item.horizon_closes_at,
                    item.payload_sha256,
                ),
            )
        ],
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _clamp(
    value: Decimal,
    *,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal:
    return max(minimum, min(maximum, value))
