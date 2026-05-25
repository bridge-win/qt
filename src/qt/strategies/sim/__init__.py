"""Simulation-style strategies (older "solution-gallery" backtest classes).

These coexist with the live signal-generator strategies in
``qt.strategies``. The signal generators (``SmartDCA``, ``Capitulation``,
``WeeklyTrend``, ``BasisCarry`` re-exported from ``qt.strategies``) are
designed for the live runner — they pull current data, emit
``Opportunity`` objects, and feed the dashboard.

The classes in this subpackage are **batch backtesters**: they take a
full OHLCV history and return an equity curve / trade list. Useful for
research, parameter tuning, and walk-forward analysis but not wired
into the live runner.

The names are differentiated to make the two paradigms unambiguous:

- ``qt.strategies.SmartDCA``      — live signal-emitter (one Opportunity/cycle)
- ``qt.strategies.sim.SmartDCABacktest`` — batch backtest over a history
"""

from qt.strategies.sim.base import StrategyResult, simulate_target_positions, vol_target_weight
from qt.strategies.sim.basis_carry import BasisCarry as BasisCarryBacktest
from qt.strategies.sim.basis_carry import BasisCarryConfig
from qt.strategies.sim.smart_dca import SmartDCA as SmartDCABacktest
from qt.strategies.sim.smart_dca import SmartDCAConfig
from qt.strategies.sim.trend_weekly import WeeklyTrend as WeeklyTrendBacktest
from qt.strategies.sim.trend_weekly import WeeklyTrendConfig

__all__ = [
    "BasisCarryBacktest",
    "BasisCarryConfig",
    "SmartDCABacktest",
    "SmartDCAConfig",
    "StrategyResult",
    "WeeklyTrendBacktest",
    "WeeklyTrendConfig",
    "simulate_target_positions",
    "vol_target_weight",
]
