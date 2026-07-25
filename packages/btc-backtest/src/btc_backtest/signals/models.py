"""Frozen normalized signal and ranking contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from btc_backtest.data.models import SHA256_PATTERN

IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]*$"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("signal timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _unique(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field} must be unique")
    return values


class SignalObservation(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    provider: str = Field(pattern=IDENTIFIER_PATTERN)
    source_type: str = Field(pattern=IDENTIFIER_PATTERN)
    symbol: str = Field(min_length=1)
    horizon: str = Field(min_length=1)
    direction: Decimal = Field(ge=-1, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    raw_value: Decimal | None = None
    observed_at: datetime
    effective_at: datetime
    expires_at: datetime
    provenance: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=SHA256_PATTERN)
    quality_flags: tuple[str, ...] = ()

    @field_validator("observed_at", "effective_at", "expires_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("direction", "confidence", "raw_value")
    @classmethod
    def require_finite_decimal(
        cls,
        value: Decimal | None,
    ) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("signal numeric values must be finite")
        return value

    @field_validator("quality_flags")
    @classmethod
    def validate_quality_flags(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not value or value.strip() != value for value in values):
            raise ValueError("quality flags must be non-empty and trimmed")
        return _unique(values, "quality_flags")

    @model_validator(mode="after")
    def validate_availability(self) -> SignalObservation:
        if self.effective_at > self.expires_at:
            raise ValueError("effective_at cannot follow expires_at")
        if self.observed_at > self.expires_at:
            raise ValueError("observed_at cannot follow expires_at")
        return self


class SignalQuery(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    start: datetime
    end: datetime
    symbol: str = Field(min_length=1)
    horizons: tuple[str, ...] = Field(min_length=1)
    source_types: tuple[str, ...] = ()
    require_historical: bool = True

    @field_validator("start", "end")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("horizons", "source_types")
    @classmethod
    def validate_filters(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not value or value.strip() != value for value in values):
            raise ValueError("signal filters must be non-empty and trimmed")
        return _unique(values, "signal filters")

    @model_validator(mode="after")
    def validate_interval(self) -> SignalQuery:
        if self.end <= self.start:
            raise ValueError("signal query end must be after start")
        return self


class SignalProviderMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(pattern=IDENTIFIER_PATTERN)
    description: str = Field(min_length=1)
    source_types: tuple[str, ...] = Field(min_length=1)
    historical: bool
    requires_credentials: bool = False

    @field_validator("source_types")
    @classmethod
    def validate_source_types(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not value or value.strip() != value for value in values):
            raise ValueError("source types must be non-empty and trimmed")
        return _unique(values, "source_types")


class SignalContributor(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    observation_id: str = Field(min_length=1)
    provider: str = Field(pattern=IDENTIFIER_PATTERN)
    source_type: str = Field(pattern=IDENTIFIER_PATTERN)
    direction: Decimal = Field(ge=-1, le=1)
    weight: Decimal = Field(ge=0, le=1)
    provenance: str = Field(min_length=1)

    @field_validator("direction", "weight")
    @classmethod
    def require_finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("contributor numeric values must be finite")
        return value


class RankedSignal(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    horizon: str = Field(min_length=1)
    direction: Decimal = Field(ge=-1, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    as_of: datetime
    contributors: tuple[SignalContributor, ...] = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("direction", "confidence")
    @classmethod
    def require_finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("ranked numeric values must be finite")
        return value

    @field_validator("contributors")
    @classmethod
    def validate_contributors(
        cls,
        values: tuple[SignalContributor, ...],
    ) -> tuple[SignalContributor, ...]:
        identifiers = tuple(value.observation_id for value in values)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("ranked contributors must be unique")
        return values
