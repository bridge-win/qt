"""Native NautilusTrader research integration.

This package deliberately loads NautilusTrader only at execution time.  QT can
therefore inspect research requests on hosts which do not have the native Rust
extension installed, but it can never silently fall back to ``EventRunner``.
"""

from qt.nautilus.adapter import CausalStrategyBridge, NativeStrategyFactory
from qt.nautilus.executor import NautilusResearchExecutor
from qt.nautilus.models import (
    DataRequirement,
    DecisionTrace,
    ExecutionAssumptions,
    NautilusArtifact,
    NautilusRunSummary,
)
from qt.nautilus.runner import (
    NativeLedgerError,
    NativeRunCancelledError,
    NautilusDataFrameRunner,
    NautilusUnavailableError,
)

__all__ = [
    "CausalStrategyBridge",
    "DataRequirement",
    "DecisionTrace",
    "ExecutionAssumptions",
    "NativeLedgerError",
    "NativeRunCancelledError",
    "NativeStrategyFactory",
    "NautilusArtifact",
    "NautilusDataFrameRunner",
    "NautilusResearchExecutor",
    "NautilusRunSummary",
    "NautilusUnavailableError",
]
