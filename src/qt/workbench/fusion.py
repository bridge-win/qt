"""Canonical, source-faithful fusion research façade.

This module intentionally exposes pure source-research functions for worker or
web API wiring. It neither calls Freqtrade CLI nor executes an exchange order.
The S2 crowding path is the stateful ``btcqt_s2_crowding_fader`` port, with its
original persistence, regime, OI, exhaustion, fill, stop, and time-stop rules.
"""

from qt.workbench.source_research import (
    BTCQT_SOURCE,
    EVOLUTION_SOURCE,
    RESEARCH_CONTRACT,
    CandidateAcceptancePolicy,
    DataQualityPolicy,
    EvolutionCandidate,
    FeatureQualityPolicy,
    LiquidityPolicy,
    PromotionPolicy,
    RegimePolicy,
    RiskPolicy,
    RobustnessPolicy,
    WalkForwardRequest,
    align_features_asof,
    build_evolution_plan,
    build_feature_matrix_from_local_artifacts,
    build_source_research_report,
    build_walk_forward_plan,
    create_crowding_fader_port,
    decide_crowding_fader,
    default_evolution_plan,
    evaluate_acceptance,
    evaluate_config_risk_gate,
    evaluate_dry_run_evidence_gate,
    evaluate_feature_matrix_quality,
    evaluate_ohlcv_data_quality,
    evaluate_ohlcv_integrity_gate,
    evaluate_orderbook_liquidity_gate,
    evaluate_promotion,
    evaluate_readiness_json_gate,
    evaluate_regime_robustness_gate,
    evaluate_trade_robustness_gate,
    evaluate_walk_forward_results,
    evolution_candidates,
    rank_catalog_backtests,
    rank_evolution_candidates,
    validate_feature_matrix_schema,
)

default_candidate_space = evolution_candidates
rank_candidates = rank_evolution_candidates
align_asof_features = align_features_asof


__all__ = [
    "BTCQT_SOURCE",
    "EVOLUTION_SOURCE",
    "RESEARCH_CONTRACT",
    "CandidateAcceptancePolicy",
    "DataQualityPolicy",
    "EvolutionCandidate",
    "FeatureQualityPolicy",
    "LiquidityPolicy",
    "PromotionPolicy",
    "RegimePolicy",
    "RiskPolicy",
    "RobustnessPolicy",
    "WalkForwardRequest",
    "align_asof_features",
    "build_evolution_plan",
    "build_feature_matrix_from_local_artifacts",
    "build_source_research_report",
    "build_walk_forward_plan",
    "create_crowding_fader_port",
    "decide_crowding_fader",
    "default_candidate_space",
    "default_evolution_plan",
    "evaluate_acceptance",
    "evaluate_config_risk_gate",
    "evaluate_dry_run_evidence_gate",
    "evaluate_feature_matrix_quality",
    "evaluate_ohlcv_data_quality",
    "evaluate_ohlcv_integrity_gate",
    "evaluate_orderbook_liquidity_gate",
    "evaluate_promotion",
    "evaluate_readiness_json_gate",
    "evaluate_regime_robustness_gate",
    "evaluate_trade_robustness_gate",
    "evaluate_walk_forward_results",
    "evolution_candidates",
    "rank_candidates",
    "rank_catalog_backtests",
    "rank_evolution_candidates",
    "validate_feature_matrix_schema",
]
