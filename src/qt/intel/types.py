"""Shared types for the Intelligence Discovery system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class IntelFinding:
    """One detected opportunity, normalized so different kinds can be ranked.

    ``net_edge_bps`` is the number that matters: expected edge in basis
    points AFTER subtracting every fee leg and a slippage buffer. Anything
    ≤ 0 never surfaces. ``confidence`` in [0,1] scales the ranking —
    a fat funding APR that has persisted for days ranks above a one-tick
    spread blip of the same magnitude.
    """

    ts: datetime
    kind: str                    # funding_carry | funding_diff | spread | basis | depeg | wick_regime
    symbol: str
    venues: tuple[str, ...]
    gross_edge_bps: float
    fees_bps: float
    net_edge_bps: float
    confidence: float            # 0..1
    reason: str                  # one plain-language sentence
    action_hint: str = "watch"   # what a human/strategy could do about it
    details: dict[str, object] = field(default_factory=dict)

    @property
    def score(self) -> float:
        return max(0.0, self.net_edge_bps) * self.confidence

    def as_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts.isoformat(),
            "kind": self.kind,
            "symbol": self.symbol,
            "venues": list(self.venues),
            "gross_edge_bps": round(self.gross_edge_bps, 2),
            "fees_bps": round(self.fees_bps, 2),
            "net_edge_bps": round(self.net_edge_bps, 2),
            "confidence": round(self.confidence, 3),
            "score": round(self.score, 2),
            "reason": self.reason,
            "action_hint": self.action_hint,
            "details": dict(self.details),
        }
