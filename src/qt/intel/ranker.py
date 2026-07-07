"""Rank and persist discovered opportunities."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

from qt.intel.scanners import Opportunity

JsonDict: TypeAlias = dict[str, object]


def rank_opportunities(opportunities: list[Opportunity], *, top_n: int = 20) -> list[Opportunity]:
    ranked = [replace(opp, score=_score(opp)) for opp in opportunities if opp.edge_bps > 0]
    ranked.sort(key=lambda opp: (opp.score, opp.edge_bps, opp.confidence), reverse=True)
    return ranked[:top_n]


def persist_opportunities(runtime_dir: str | Path, opportunities: list[Opportunity]) -> Path:
    out_dir = Path(runtime_dir) / "intel"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "opportunities.json"
    payload: JsonDict = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "count": len(opportunities),
        "opportunities": [opp.as_dict() for opp in opportunities],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)
    return path


def read_opportunities(runtime_dir: str | Path) -> JsonDict:
    path = Path(runtime_dir) / "intel" / "opportunities.json"
    if not path.exists():
        return {"generated_at": None, "count": 0, "opportunities": []}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"generated_at": None, "count": 0, "opportunities": []}
    if not isinstance(raw, dict):
        return {"generated_at": None, "count": 0, "opportunities": []}
    opportunities = raw.get("opportunities")
    return {
        "generated_at": raw.get("generated_at"),
        "count": raw.get("count", 0),
        "opportunities": opportunities if isinstance(opportunities, list) else [],
    }


def _score(opp: Opportunity) -> float:
    capacity_score = min(1.0, math.log10(max(opp.capacity_usd, 1.0)) / 5.0)
    return max(0.0, opp.edge_bps) * max(0.0, min(1.0, opp.confidence)) * capacity_score
