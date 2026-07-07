"""Rank findings by (net edge x confidence) and persist for the dashboard."""

from __future__ import annotations

import json
from pathlib import Path

from qt.intel.types import IntelFinding


def rank_findings(findings: list[IntelFinding], top_n: int = 20) -> list[IntelFinding]:
    """Highest score first. Wick-regime findings (score 0 by design — they're
    context, not a priced edge) are appended after priced findings."""

    priced = [f for f in findings if f.net_edge_bps > 0]
    context = [f for f in findings if f.net_edge_bps <= 0]
    priced.sort(key=lambda f: f.score, reverse=True)
    return (priced + context)[:top_n]


def write_findings(
    findings: list[IntelFinding],
    runtime_dir: str | Path = "data/runtime",
) -> Path:
    """Persist ranked findings to <runtime_dir>/intel/opportunities.json."""

    out_dir = Path(runtime_dir) / "intel"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "opportunities.json"
    payload = {"findings": [f.as_dict() for f in findings]}
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def read_findings(runtime_dir: str | Path = "data/runtime") -> list[dict[str, object]]:
    """Load the last persisted findings (dashboard-side)."""

    path = Path(runtime_dir) / "intel" / "opportunities.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    found = data.get("findings")
    return found if isinstance(found, list) else []
