"""Public custom strategy protocol and point-in-time context models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, Protocol, cast, runtime_checkable

import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from btc_backtest.data.models import DataManifest, Timeframe
from btc_backtest.engine.models import (
    BacktestSpec,
    Fill,
    InstrumentKind,
    Order,
    OrderIntent,
    PortfolioSnapshot,
)


class StrategyMetadata(BaseModel):
    """Stable identity, capabilities, and data requirements for a strategy."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(min_length=1)
    api_version: Literal["1"] = "1"
    description: str = Field(min_length=1)
    warmup_bars: int = Field(ge=0)
    supported_timeframes: tuple[Timeframe, ...] = Field(min_length=1)
    supported_instruments: tuple[InstrumentKind, ...] = (
        InstrumentKind.SPOT,
    )
    required_fields: tuple[str, ...] = (
        "open",
        "high",
        "low",
        "close",
        "volume",
    )
    signal_dependencies: tuple[str, ...] = ()
    min_weight: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    max_weight: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    parameter_schema: Mapping[str, object] = Field(
        default_factory=dict,
        validate_default=True,
    )

    @field_validator(
        "supported_timeframes",
        "supported_instruments",
        "required_fields",
        "signal_dependencies",
    )
    @classmethod
    def require_unique_capabilities(
        cls,
        values: tuple[object, ...],
    ) -> tuple[object, ...]:
        if len(set(values)) != len(values):
            raise ValueError("strategy capabilities must be unique")
        return values

    @field_validator("parameter_schema")
    @classmethod
    def freeze_parameter_schema(
        cls,
        value: Mapping[str, object],
    ) -> Mapping[str, object]:
        return MappingProxyType(dict(value))

    @field_serializer("parameter_schema")
    def serialize_parameter_schema(
        self,
        value: Mapping[str, object],
    ) -> dict[str, object]:
        return dict(value)

    @model_validator(mode="after")
    def validate_weight_bounds(self) -> StrategyMetadata:
        if not self.min_weight.is_finite() or not self.max_weight.is_finite():
            raise ValueError("strategy weight bounds must be finite")
        if self.min_weight > self.max_weight:
            raise ValueError("strategy min_weight cannot exceed max_weight")
        return self


class InitializationContext(BaseModel):
    """Read-only data available once before the first bar."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    spec: BacktestSpec
    data_manifests: tuple[DataManifest, ...]
    parameters: Mapping[str, object] = Field(
        default_factory=dict,
        validate_default=True,
    )

    @field_validator("parameters")
    @classmethod
    def freeze_parameters(
        cls,
        value: Mapping[str, object],
    ) -> Mapping[str, object]:
        return MappingProxyType(dict(value))


class StrategyContext(BaseModel):
    """Isolated market history ending at the active simulation timestamp."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    timestamp: datetime
    bars: pd.DataFrame
    auxiliary: Mapping[str, pd.DataFrame] = Field(
        default_factory=dict,
        validate_default=True,
    )
    portfolio: PortfolioSnapshot
    open_orders: tuple[Order, ...] = ()
    signals: tuple[object, ...] = ()
    parameters: Mapping[str, object] = Field(
        default_factory=dict,
        validate_default=True,
    )

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("strategy timestamp must be timezone-aware")
        return value

    @field_validator("bars")
    @classmethod
    def isolate_bars(cls, value: pd.DataFrame) -> pd.DataFrame:
        return _isolated_frame(value, "primary")

    @field_validator("auxiliary")
    @classmethod
    def isolate_auxiliary(
        cls,
        value: Mapping[str, pd.DataFrame],
    ) -> Mapping[str, pd.DataFrame]:
        return MappingProxyType(
            {
                name: _isolated_frame(frame, f"auxiliary {name}")
                for name, frame in value.items()
            }
        )

    @field_validator("parameters")
    @classmethod
    def freeze_parameters(
        cls,
        value: Mapping[str, object],
    ) -> Mapping[str, object]:
        return MappingProxyType(dict(value))

    @model_validator(mode="after")
    def reject_future_rows(self) -> StrategyContext:
        active = pd.Timestamp(self.timestamp)
        if not self.bars.empty and (self.bars.index > active).any():
            raise ValueError("primary strategy history contains future rows")
        for name, frame in self.auxiliary.items():
            if not frame.empty and (frame.index > active).any():
                raise ValueError(
                    f"auxiliary strategy history {name} contains future rows"
                )
        return self

    @property
    def current_bar(self) -> pd.Series:
        if self.bars.empty:
            raise IndexError("strategy context has no bars")
        return cast(pd.Series, self.bars.iloc[-1].copy())


class FinalizationContext(BaseModel):
    """Read-only terminal state supplied after the last simulation bar."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    spec: BacktestSpec
    portfolio: PortfolioSnapshot
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    warnings: tuple[str, ...] = ()


@runtime_checkable
class Strategy(Protocol):
    metadata: StrategyMetadata

    def initialize(self, context: InitializationContext) -> None: ...

    def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]: ...

    def finalize(self, context: FinalizationContext) -> None: ...


def _isolated_frame(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(f"{label} strategy history requires a DatetimeIndex")
    if frame.index.tz is None:
        raise ValueError(f"{label} strategy history must be timezone-aware")
    if not frame.index.is_monotonic_increasing or not frame.index.is_unique:
        raise ValueError(
            f"{label} strategy history timestamps must be unique and ascending"
        )
    return frame.copy(deep=True)
