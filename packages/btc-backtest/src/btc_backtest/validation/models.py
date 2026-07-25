"""Immutable validation configuration and split result contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class SplitMode(str, Enum):
    EXPANDING = "expanding"
    ROLLING = "rolling"
    PURGED = "purged"


class Window(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    timestamps: tuple[datetime, ...] = ()

    @field_validator("start", "end")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("timestamps")
    @classmethod
    def require_utc_timestamps(
        cls,
        values: tuple[datetime, ...],
    ) -> tuple[datetime, ...]:
        normalized = tuple(_utc(value) for value in values)
        if tuple(sorted(normalized)) != normalized:
            raise ValueError("window timestamps must be sorted")
        if len(set(normalized)) != len(normalized):
            raise ValueError("window timestamps must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_interval(self) -> Window:
        if self.end < self.start:
            raise ValueError("window end must be after or equal to start")
        outside = [
            value
            for value in self.timestamps
            if value < self.start or value >= self.end
        ]
        if outside:
            raise ValueError("window timestamps must be inside interval")
        return self


class ValidationSplit(BaseModel):
    model_config = ConfigDict(frozen=True)

    train: Window
    purge: Window
    test: Window
    next_eligible_start: datetime

    @field_validator("next_eligible_start")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_ordering(self) -> ValidationSplit:
        if self.train.end > self.purge.start:
            raise ValueError("train window cannot overlap purge window")
        if self.purge.end > self.test.start:
            raise ValueError("purge window cannot overlap test window")
        if self.next_eligible_start < self.test.end:
            raise ValueError(
                "next eligible start cannot precede test window end"
            )
        return self


class ValidationSpec(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    mode: SplitMode = SplitMode.PURGED
    selection_end: datetime
    final_test_start: datetime
    final_test_end: datetime
    train_bars: int = Field(default=365, gt=0)
    test_bars: int = Field(default=90, gt=0)
    purge_bars: int = Field(default=0, ge=0)
    embargo_bars: int = Field(default=0, ge=0)
    objective: str = Field(default="sharpe", min_length=1)
    seed: int = 7

    @field_validator(
        "selection_end",
        "final_test_start",
        "final_test_end",
    )
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_windows(self) -> ValidationSpec:
        if self.final_test_end <= self.final_test_start:
            raise ValueError("final test end must be after start")
        if self.selection_end >= self.final_test_start:
            raise ValueError(
                "selection window cannot inspect the final test window"
            )
        return self


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    spec: ValidationSpec
    splits: tuple[ValidationSplit, ...]
    selected_parameters: tuple[dict[str, object], ...] = ()
    warnings: tuple[str, ...] = ()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("validation timestamps must be timezone-aware UTC")
    normalized = value.astimezone(timezone.utc)
    if value.utcoffset() != timezone.utc.utcoffset(normalized):
        raise ValueError("validation timestamps must be UTC")
    return normalized
