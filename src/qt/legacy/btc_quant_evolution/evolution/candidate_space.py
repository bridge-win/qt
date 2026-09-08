# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvolutionCandidate:
    id: str
    feature_weights: dict[str, float]
    entry_threshold: float
    exit_threshold: float
    atr_multiple: float
    risk_fraction: float


def default_candidate_space() -> list[EvolutionCandidate]:
    """Return the small deterministic seed space used for local evolution."""
    return [
        EvolutionCandidate(
            "ms-v001",
            {"technical": 1.0, "sentiment": 0.5, "derivatives": 0.5, "onchain": 0.5},
            1.0,
            0.2,
            2.5,
            0.005,
        ),
        EvolutionCandidate(
            "ms-v002",
            {"technical": 0.8, "sentiment": 0.8, "derivatives": 0.6, "onchain": 0.4},
            1.2,
            0.25,
            2.0,
            0.004,
        ),
        EvolutionCandidate(
            "ms-v003",
            {"technical": 0.6, "sentiment": 1.0, "derivatives": 0.8, "onchain": 0.6},
            1.1,
            0.15,
            3.0,
            0.003,
        ),
    ]

