# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import math
from typing import Final


_METRICS: Final[tuple[tuple[str, bool], ...]] = (
    ("calmar", True),
    ("profit_factor", True),
    ("sortino", True),
    ("total_trades", True),
    ("max_drawdown_pct", False),
)


def rank_evolution_results(results: list[dict[str, object]]) -> list[dict[str, object]]:
    """Rank candidates using train/validation selection evidence only."""
    return sorted(results, key=_ranking_key)


def _ranking_key(result: dict[str, object]) -> tuple[object, ...]:
    selection = result.get("selection")
    passed = isinstance(selection, dict) and selection.get("passed") is True
    metrics = selection.get("metrics") if isinstance(selection, dict) else None
    metric_values = []
    for name, descending in _METRICS:
        value = metrics.get(name) if isinstance(metrics, dict) else None
        numeric = float(value) if _is_finite_number(value) else 0.0
        metric_values.append(-numeric if descending else numeric)
    candidate_id = result.get("candidate_id")
    return (0 if passed else 1, *metric_values, str(candidate_id) if candidate_id is not None else "")


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

