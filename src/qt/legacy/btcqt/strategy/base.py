# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Strategy interface. Strategies are pure decision-makers:
they see MarketState snapshots + their own position, and emit Intents.
Executors (backtest / paper / live) own order plumbing and fill detection.
The same strategy code runs everywhere — no backtest-vs-live drift.
"""
from __future__ import annotations

from ..models import Fill, Intent, MarketState, Position


class Strategy:
    name: str = "base"

    def __init__(self):
        self.pos = Position(strategy=self.name)

    def on_state(self, state: MarketState, equity: float) -> list[Intent]:
        """Called at every 1m candle close (after fills for that candle are applied)."""
        raise NotImplementedError

    def on_fill(self, fill: Fill, state: MarketState) -> tuple[list[Intent], float]:
        """Called when one of this strategy's orders fills.
        Returns (follow-up intents, realized pnl before fees)."""
        pnl = self.pos.apply_fill(fill)
        return [], pnl


