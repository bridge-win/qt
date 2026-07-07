"""Intelligence Discovery: scan markets for ranked, net-of-fees opportunities.

Scanners *detect and alert* — they never execute. Execution belongs to the
slow strategies (carry, dca, wick) or to the human (depeg trades). See
docs/RESEARCH-EARNING.md for the evidence behind each scanner.
"""

from qt.intel.ranker import rank_findings, write_findings
from qt.intel.types import IntelFinding

__all__ = ["IntelFinding", "rank_findings", "write_findings"]
