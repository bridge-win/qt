"""Solution-gallery strategies (DCA / Capitulation / Trend / Carry).

Each strategy is a self-contained *signal generator* with its own YAML
config under ``config/strategies/``. The multi-strategy runner spawns
one thread per enabled strategy and writes a per-strategy heartbeat
under ``data/runtime/strategies/<name>.json``; the dashboard reads
those files for the ``/strategy/<name>`` sub-routes.

Public surface:

- :class:`Strategy` — abstract base
- :class:`StrategyConfig` — YAML schema (per-strategy ``params`` go inside)
- :class:`Opportunity`, :class:`EvaluationResult` — output types
- :func:`load_strategy_configs`, :func:`build_strategies` — loader helpers
- :func:`start_all_strategies`, :func:`run_strategy_forever` — runner
- ``REGISTRY`` — name → class map
"""

from qt.strategies.base import (
    EvaluationResult,
    Opportunity,
    Strategy,
    StrategyConfig,
)
from qt.strategies.capitulation import Capitulation
from qt.strategies.carry import BasisCarry
from qt.strategies.dca import SmartDCA
from qt.strategies.loader import build_strategies, load_strategy_configs
from qt.strategies.registry import REGISTRY, strategy_class
from qt.strategies.runner import (
    run_strategy_forever,
    start_all_strategies,
    strategy_state_dir,
    strategy_state_path,
    wait_for_shutdown,
)
from qt.strategies.trend import WeeklyTrend

__all__ = [
    "REGISTRY",
    "BasisCarry",
    "Capitulation",
    "EvaluationResult",
    "Opportunity",
    "SmartDCA",
    "Strategy",
    "StrategyConfig",
    "WeeklyTrend",
    "build_strategies",
    "load_strategy_configs",
    "run_strategy_forever",
    "start_all_strategies",
    "strategy_class",
    "strategy_state_dir",
    "strategy_state_path",
    "wait_for_shutdown",
]
