"""Strategy base types.

A `Strategy` is a *signal generator* — it pulls data, computes an
opinion, and (when its trigger fires) emits an `Opportunity` describing
an actionable trade idea. Strategies are intentionally not executors;
they exist so a retail operator can be **notified** when their criteria
are met and decide whether to act.

The multi-strategy runner (``qt.strategies.runner``) supervises one
thread per enabled strategy, each writing a durable heartbeat under
``data/runtime/strategies/<name>.json``. The dashboard reads those
files to render the per-strategy sub-routes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from qt.core.config import Settings

Action = Literal["buy", "sell", "open", "close", "watch"]


class StrategyConfig(BaseModel):
    """Base config every strategy YAML must extend.

    ``interval_seconds`` is how often the runner re-evaluates this
    strategy; ``enabled`` lets you flip it off without removing the
    file. ``min_alert_severity`` controls whether notifications are
    actually sent (``info``, ``warning``, ``critical``).
    """

    name: str = ""                      # auto-filled by loader from filename
    enabled: bool = True
    interval_seconds: int = 3600
    description: str = ""
    min_alert_severity: Literal["info", "warning", "critical"] = "critical"
    # The actual signal-specific params live on subclasses, defined per strategy.
    params: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class Opportunity:
    """A single actionable signal emitted by a strategy.

    ``confidence`` is a strategy-defined number in ``[0, 1]`` — used
    only for ranking in the dashboard. ``details`` is free-form and
    passed straight through to the alert sink.
    """

    ts: datetime
    action: Action
    confidence: float
    reason: str
    details: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts.isoformat(),
            "action": self.action,
            "confidence": float(self.confidence),
            "reason": self.reason,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class EvaluationResult:
    """Returned from ``Strategy.evaluate``: what the dashboard renders
    every cycle, regardless of whether an opportunity fired."""

    ts: datetime
    opportunity: Opportunity | None
    metrics: dict[str, object]          # latest signal values for the dashboard
    notes: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts.isoformat(),
            "opportunity": self.opportunity.as_dict() if self.opportunity else None,
            "metrics": dict(self.metrics),
            "notes": self.notes,
        }


class Strategy(ABC):
    """Abstract base for every gallery strategy.

    Subclasses implement ``fetch_data`` (pull whatever inputs the
    evaluation needs) and ``evaluate`` (compute the signal). The runner
    handles scheduling, heartbeat persistence, error backoff, and
    alerting on opportunities.
    """

    name: str = "base"
    description: str = ""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        # Subclasses are free to attach a stronger-typed dataclass here
        # for ergonomic access to their own params.

    @abstractmethod
    def fetch_data(self, settings: Settings) -> dict[str, Any]:
        """Pull the data this strategy needs for one evaluation cycle.

        Implementations may fall back to cached / on-disk data. Returning
        ``{}`` is OK — ``evaluate`` should handle the empty case
        gracefully (typically by returning a "watch" result without an
        opportunity).
        """

    @abstractmethod
    def evaluate(self, data: dict[str, Any]) -> EvaluationResult:
        """Compute the strategy's current opinion from ``data``.

        Must always return an ``EvaluationResult``; ``opportunity`` is
        ``None`` when nothing fires. Must not raise on missing data —
        the runner already provides supervised exception handling, but
        treating missing inputs as a soft "watch" is the right behavior
        for users who haven't subscribed to all data providers.
        """


__all__ = [
    "Action",
    "EvaluationResult",
    "Opportunity",
    "Strategy",
    "StrategyConfig",
]
