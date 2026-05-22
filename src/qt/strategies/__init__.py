"""Solution-gallery strategies for "low-cost, low-frequency" retail crypto quant.

Four self-contained, independently runnable strategies derived from the
research summarized in `solution2.md`:

- ``smart_dca.SmartDCA``         — volatility-aware weekly DCA accumulator.
- ``capitulation.Capitulation``  — multi-factor extreme-event buyer.
- ``trend_weekly.WeeklyTrend``   — Faber/Clenow-style weekly trend follower.
- ``basis_carry.BasisCarry``     — market-neutral funding-rate carry trade.

Each strategy ships its own docstring with: signal description, parameter
defaults, the published research it borrows from, and known failure modes.
"""

from qt.strategies.base import StrategyResult, simulate_target_positions
from qt.strategies.basis_carry import BasisCarry, BasisCarryConfig
from qt.strategies.capitulation import Capitulation, CapitulationConfig
from qt.strategies.smart_dca import SmartDCA, SmartDCAConfig
from qt.strategies.trend_weekly import WeeklyTrend, WeeklyTrendConfig

__all__ = [
    "BasisCarry",
    "BasisCarryConfig",
    "Capitulation",
    "CapitulationConfig",
    "SmartDCA",
    "SmartDCAConfig",
    "StrategyResult",
    "WeeklyTrend",
    "WeeklyTrendConfig",
    "simulate_target_positions",
]
