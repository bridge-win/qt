# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Bounded candidate evolution and promotion helpers."""

from qt.legacy.btc_quant_evolution.evolution.candidate_space import EvolutionCandidate, default_candidate_space
from qt.legacy.btc_quant_evolution.evolution.promotion_gate import PromotionPolicy, evaluate_candidate_promotion
from qt.legacy.btc_quant_evolution.evolution.scorer import rank_evolution_results

__all__ = [
    "EvolutionCandidate",
    "PromotionPolicy",
    "default_candidate_space",
    "evaluate_candidate_promotion",
    "rank_evolution_results",
]

