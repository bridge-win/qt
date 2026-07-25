"""Immutable data request, provenance, and dataset models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Timeframe = Literal["1h", "1d"]
SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _is_aligned(value: datetime, timeframe: Timeframe) -> bool:
    if value.minute != 0 or value.second != 0 or value.microsecond != 0:
        return False
    return timeframe == "1h" or value.hour == 0


class DataRequest(BaseModel):
    """A closed-open request for normalized market data."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    provider: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: Timeframe
    start: datetime
    end: datetime
    market: str = Field(default="spot", min_length=1)
    require_real: bool = True
    require_complete: bool = True
    max_missing_ratio: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("start", "end")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def validate_interval(self) -> DataRequest:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if not _is_aligned(self.start, self.timeframe) or not _is_aligned(
            self.end,
            self.timeframe,
        ):
            raise ValueError(f"start and end must be aligned to {self.timeframe}")
        return self


class DataGap(BaseModel):
    """A contiguous sequence of absent bars."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    missing_bars: int = Field(gt=0)

    @field_validator("start", "end")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def validate_interval(self) -> DataGap:
        if self.end <= self.start:
            raise ValueError("gap end must be after start")
        return self


class DataSegment(BaseModel):
    """Provenance for one contiguous provider segment."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    provider: str = Field(min_length=1)
    market: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: Timeframe
    start: datetime
    end: datetime
    real_data: bool
    normalized_sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("start", "end")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def validate_interval(self) -> DataSegment:
        if self.end <= self.start:
            raise ValueError("segment end must be after start")
        return self


class DataManifest(BaseModel):
    """Complete provenance and coverage metadata for a normalized dataset."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1"] = "1"
    provider: str = Field(min_length=1)
    market: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: Timeframe
    requested_start: datetime
    requested_end: datetime
    delivered_start: datetime
    delivered_end: datetime
    retrieved_at: datetime
    real_data: bool
    raw_sha256: tuple[str, ...] = Field(min_length=1)
    normalized_sha256: str = Field(pattern=SHA256_PATTERN)
    gaps: tuple[DataGap, ...] = ()
    segments: tuple[DataSegment, ...] = ()

    @field_validator(
        "requested_start",
        "requested_end",
        "delivered_start",
        "delivered_end",
        "retrieved_at",
    )
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @field_validator("raw_sha256")
    @classmethod
    def validate_raw_fingerprints(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(value) != 64 or value.lower() != value for value in values):
            raise ValueError("raw sha256 values must be 64 lowercase hexadecimal characters")
        if any(character not in "0123456789abcdef" for value in values for character in value):
            raise ValueError("raw sha256 values must be 64 lowercase hexadecimal characters")
        return values

    @model_validator(mode="after")
    def validate_intervals(self) -> DataManifest:
        if self.requested_end <= self.requested_start:
            raise ValueError("requested end must be after start")
        if self.delivered_end <= self.delivered_start:
            raise ValueError("delivered end must be after start")
        if self.delivered_start < self.requested_start or self.delivered_end > self.requested_end:
            raise ValueError("delivered interval must be inside requested interval")
        return self


@dataclass(frozen=True)
class MarketDataset:
    """A normalized OHLCV frame paired with immutable provenance."""

    frame: pd.DataFrame
    manifest: DataManifest


@dataclass(frozen=True)
class MarketBundle:
    """One primary market dataset plus named auxiliary datasets."""

    primary: MarketDataset
    auxiliary: Mapping[str, MarketDataset]

    def __post_init__(self) -> None:
        object.__setattr__(self, "auxiliary", MappingProxyType(dict(self.auxiliary)))
