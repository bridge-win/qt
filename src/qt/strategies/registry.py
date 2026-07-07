"""Name → Strategy class registry.

Keeps the strategy-name mapping in one place so the loader, the dashboard,
and the runner don't have to import every concrete strategy module.
"""

from __future__ import annotations

from qt.strategies.base import Strategy
from qt.strategies.capitulation import Capitulation
from qt.strategies.carry import BasisCarry
from qt.strategies.dca import SmartDCA
from qt.strategies.trend import WeeklyTrend
from qt.strategies.wick_catcher import WickCatcher

REGISTRY: dict[str, type[Strategy]] = {
    SmartDCA.name: SmartDCA,
    Capitulation.name: Capitulation,
    WeeklyTrend.name: WeeklyTrend,
    BasisCarry.name: BasisCarry,
    WickCatcher.name: WickCatcher,
}


def _register_extensions() -> None:
    """Register strategies that live outside qt.strategies.

    Imported lazily: qt.intel.strategy itself imports qt.strategies.base,
    so a module-level import here would be circular.
    """
    from qt.intel.strategy import IntelDiscovery

    REGISTRY.setdefault(IntelDiscovery.name, IntelDiscovery)


def strategy_class(name: str) -> type[Strategy]:
    _register_extensions()
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[name]


__all__ = ["REGISTRY", "strategy_class"]
