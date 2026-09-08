"""Immutable native-engine inputs, traces, and artifact metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path


class DataRequirement(str, Enum):
    """Minimum market evidence required for an execution assumption."""

    BARS = "bars"
    TRADE_TICKS = "trade_ticks"
    ORDER_BOOK = "order_book"


@dataclass(frozen=True)
class ExecutionAssumptions:
    """Native matching controls; no custom QT fill simulation is involved."""

    initial_cash: Decimal
    maker_fee: Decimal = Decimal("0.001")
    taker_fee: Decimal = Decimal("0.001")
    # Nautilus' native bar fill model can apply exactly one price tick, not a
    # fixed percentage of the price.
    slippage_bps: Decimal = Decimal("0")
    one_tick_slippage: bool = False
    limit_fill_probability: float = 1.0
    slippage_probability: float = 0.0
    latency_nanos: int = 0
    seed: int = 7
    queue_position: bool = False
    liquidity_consumption: bool = False
    leverage: Decimal = Decimal("1")
    # Instrument maintenance fraction and engine trigger ratio are separate.
    # Nautilus applies the former through the margin model and compares the
    # result against the latter; passing one value to both changes the
    # liquidation threshold.
    maintenance_margin: Decimal = Decimal("0.05")
    liquidation_trigger_ratio: Decimal = Decimal("1")
    funding_bps: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        decimal_values = {
            "initial_cash": self.initial_cash,
            "maker_fee": self.maker_fee,
            "taker_fee": self.taker_fee,
            "slippage_bps": self.slippage_bps,
            "leverage": self.leverage,
            "maintenance_margin": self.maintenance_margin,
            "liquidation_trigger_ratio": self.liquidation_trigger_ratio,
            "funding_bps": self.funding_bps,
        }
        invalid = [name for name, value in decimal_values.items() if not value.is_finite()]
        if invalid:
            raise ValueError("native decimal assumptions must be finite: " + ", ".join(invalid))
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.maker_fee < 0 or self.taker_fee < 0 or self.slippage_bps < 0:
            raise ValueError("fees and slippage must be non-negative")
        if self.slippage_bps != 0:
            raise ValueError(
                "slippage_bps is not representable by Nautilus' native bar fill model; "
                "use one_tick_slippage or provide tick/book data",
            )
        if self.funding_bps != 0:
            raise ValueError(
                "funding_bps requires timestamped funding-rate input; it cannot be synthesized",
            )
        if not 0 <= self.limit_fill_probability <= 1:
            raise ValueError("limit_fill_probability must be within [0, 1]")
        if not 0 <= self.slippage_probability <= 1:
            raise ValueError("slippage_probability must be within [0, 1]")
        if self.latency_nanos < 0:
            raise ValueError("latency_nanos must be non-negative")
        if not Decimal("1") <= self.leverage <= Decimal("3"):
            raise ValueError("research leverage must be bounded within [1x, 3x]")
        if not Decimal("0") < self.maintenance_margin <= Decimal("1"):
            raise ValueError("maintenance_margin must be within (0, 1]")
        if not Decimal("0") < self.liquidation_trigger_ratio <= Decimal("1"):
            raise ValueError("liquidation_trigger_ratio must be within (0, 1]")

    @property
    def data_requirement(self) -> DataRequirement:
        if self.queue_position or self.liquidity_consumption:
            return DataRequirement.ORDER_BOOK
        return DataRequirement.BARS


@dataclass(frozen=True)
class DecisionTrace:
    """A causal observation of a strategy decision, including non-decisions."""

    timestamp: datetime
    strategy_id: str
    observed_rows: int
    intent_count: int
    reasons: tuple[str, ...]
    no_trade_cause: str | None
    observed_values: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("trace timestamps must be timezone-aware")
        if self.observed_rows < 0 or self.intent_count < 0:
            raise ValueError("trace counts must be non-negative")
        if self.intent_count == 0 and self.no_trade_cause is None:
            raise ValueError("a no-trade trace requires an explicit cause")
        if self.intent_count > 0 and self.no_trade_cause is not None:
            raise ValueError("a submitted-intent trace cannot have a no-trade cause")
        if len({key for key, _value in self.observed_values}) != len(self.observed_values):
            raise ValueError("trace observation keys must be unique")


@dataclass(frozen=True)
class NautilusArtifact:
    """Content-addressed local artifact produced by the native engine."""

    name: str
    path: Path
    sha256: str
    media_type: str
    size_bytes: int
    rows: int | None = None


@dataclass(frozen=True)
class NautilusRunSummary:
    """The v3 workbench-facing, immutable summary of one native run."""

    run_id: str
    engine: str
    engine_version: str
    strategy_id: str
    data_requirement: DataRequirement
    started_at: datetime
    finished_at: datetime
    total_events: int
    total_orders: int
    total_positions: int
    reports: tuple[NautilusArtifact, ...]
    traces: tuple[DecisionTrace, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.engine != "nautilus_trader":
            raise ValueError("native summaries must identify nautilus_trader")
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("summary timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
