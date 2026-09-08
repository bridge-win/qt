"""Pure adapters over the preserved btc-quant evolution research sources.

No function here starts Freqtrade, queues work, contacts an exchange, or writes
except ``build_feature_matrix_from_local_artifacts``.  That one explicitly
delegates to the source local-artifact builder and needs an output path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pandas as pd

from qt.legacy.btc_quant_evolution.candidate_acceptance import (
    CandidateAcceptancePolicy,
    evaluate_candidate_acceptance,
)
from qt.legacy.btc_quant_evolution.catalog_backtest import rank_catalog_results
from qt.legacy.btc_quant_evolution.data_quality import DataQualityPolicy, evaluate_data_quality
from qt.legacy.btc_quant_evolution.dry_run_evidence import evaluate_dry_run_evidence
from qt.legacy.btc_quant_evolution.evolution.candidate_space import (
    EvolutionCandidate,
    default_candidate_space,
)
from qt.legacy.btc_quant_evolution.evolution.promotion_gate import (
    PromotionPolicy,
    evaluate_candidate_promotion,
)
from qt.legacy.btc_quant_evolution.evolution.scorer import rank_evolution_results
from qt.legacy.btc_quant_evolution.evolution.walk_forward_evolution import (
    EvolutionSplit,
    build_evolution_split_plan,
    default_pre_2026_splits,
)
from qt.legacy.btc_quant_evolution.features.align import align_asof_features
from qt.legacy.btc_quant_evolution.features.feature_matrix import (
    build_feature_matrix,
    validate_strategy_schema,
)
from qt.legacy.btc_quant_evolution.features.quality import (
    FeatureQualityPolicy,
    evaluate_feature_quality,
)
from qt.legacy.btc_quant_evolution.liquidity_evidence import (
    LiquidityPolicy,
    evaluate_orderbook_liquidity,
)
from qt.legacy.btc_quant_evolution.ohlcv_integrity import evaluate_ohlcv_integrity
from qt.legacy.btc_quant_evolution.readiness import evaluate_json_gate
from qt.legacy.btc_quant_evolution.regime import RegimePolicy, evaluate_regime_robustness
from qt.legacy.btc_quant_evolution.risk_evidence import RiskPolicy, evaluate_config_risk
from qt.legacy.btc_quant_evolution.robustness import RobustnessPolicy, evaluate_trade_robustness
from qt.legacy.btc_quant_evolution.walk_forward import (
    WalkForwardRequest,
    build_walk_forward_splits,
    evaluate_walk_forward,
)
from qt.strategy_ports.btcqt import (
    BTCQT_SOURCE,
    BtcqtStrategyPort,
    CausalStateTimeline,
    PortDecision,
    create_btcqt_port,
)
from qt.strategy_ports.btcqt import SourceIdentity as PortSourceIdentity

EVOLUTION_SOURCE = PortSourceIdentity(
    repository="/Users/kwt/x/btc-quant/.worktrees/multisource-evolution",
    commit="e0b47ca37910ea30de227c214c834b4796f7a281",
    module="qt.legacy.btc_quant_evolution",
    source_version="multisource-evolution",
)
DEFAULT_PROMOTION_POLICY = PromotionPolicy()
DEFAULT_ACCEPTANCE_POLICY = CandidateAcceptancePolicy()

# This is intentionally data rather than a queue/job schema: workers invoke the
# named functions directly and retain ownership of any artifact persistence.
RESEARCH_CONTRACT: Final[dict[str, dict[str, object]]] = {
    "evolution_candidates": {
        "inputs": [],
        "output": "tuple[EvolutionCandidate, ...] from the original deterministic seed space",
        "source": "evolution.candidate_space.default_candidate_space",
    },
    "rank_evolution_candidates": {
        "inputs": ["results: list[selection artifact]"],
        "output": "source-ranked selection artifacts",
        "source": "evolution.scorer.rank_evolution_results",
    },
    "evaluate_promotion": {
        "inputs": ["candidate_id", "final-OOS reports", "PromotionPolicy"],
        "output": "source final-OOS promotion decision",
        "source": "evolution.promotion_gate.evaluate_candidate_promotion",
    },
    "evaluate_acceptance": {
        "inputs": ["rolling-split payload", "root_dir", "CandidateAcceptancePolicy"],
        "output": "source parameter-stability/bootstrap acceptance decision",
        "source": "candidate_acceptance.evaluate_candidate_acceptance",
    },
    "build_walk_forward_plan": {
        "inputs": ["WalkForwardRequest"],
        "output": "causal train/test timerange dictionaries",
        "source": "walk_forward.build_walk_forward_splits",
    },
    "evaluate_walk_forward_results": {
        "inputs": ["completed split artifacts with production/robustness/regime gates"],
        "output": "source walk-forward aggregate and failure reasons",
        "source": "walk_forward.evaluate_walk_forward",
    },
    "align_features_asof": {
        "inputs": ["candles.date", "features.available_at", "optional tolerance"],
        "output": "feature frame merged only from rows available at or before each candle",
        "source": "features.align.align_asof_features",
    },
    "evaluate_feature_matrix_quality": {
        "inputs": ["feature frame", "FeatureQualityPolicy"],
        "output": "source feature gap/staleness/lookahead quality decision",
        "source": "features.quality.evaluate_feature_quality",
    },
    "evaluate_ohlcv_data_quality": {
        "inputs": ["coverage artifacts", "pair", "timeframe", "DataQualityPolicy", "as_of"],
        "output": "source OHLCV coverage/staleness decision",
        "source": "data_quality.evaluate_data_quality",
    },
    "evaluate_ohlcv_integrity_gate": {
        "inputs": ["OHLCV rows", "timeframe"],
        "output": "source duplicate/gap/candle-integrity decision",
        "source": "ohlcv_integrity.evaluate_ohlcv_integrity",
    },
    "evaluate_trade_robustness_gate": {
        "inputs": ["completed trade artifacts", "RobustnessPolicy"],
        "output": "source cost-stress/loss-cluster robustness gate",
        "source": "robustness.evaluate_trade_robustness",
    },
    "evaluate_regime_robustness_gate": {
        "inputs": ["causal OHLCV candles", "completed trade artifacts", "RegimePolicy"],
        "output": "source trend/volatility regime gate",
        "source": "regime.evaluate_regime_robustness",
    },
    "evaluate_config_risk_gate": {
        "inputs": ["candidate config", "RiskPolicy"],
        "output": "source spot/stop/drawdown risk decision",
        "source": "risk_evidence.evaluate_config_risk",
    },
    "evaluate_orderbook_liquidity_gate": {
        "inputs": ["caller-supplied order-book snapshot", "LiquidityPolicy"],
        "output": "source depth/impact liquidity decision",
        "source": "liquidity_evidence.evaluate_orderbook_liquidity",
    },
    "evaluate_dry_run_evidence_gate": {
        "inputs": ["dry-run config", "log text", "bounded launch/evaluation timestamps"],
        "output": "source dry-run duration/future-timestamp decision",
        "source": "dry_run_evidence.evaluate_dry_run_evidence",
    },
    "evaluate_readiness_json_gate": {
        "inputs": ["parsed artifact payload", "key path", "source name"],
        "output": "source readiness decision for an already-produced gate",
        "source": "readiness.evaluate_json_gate",
    },
    "rank_catalog_backtests": {
        "inputs": ["completed catalog result artifacts"],
        "output": "source readiness-aware catalog ranking",
        "source": "catalog_backtest.rank_catalog_results",
    },
    "build_feature_matrix_from_local_artifacts": {
        "inputs": ["local root_dir/OHLCV", "external artifact paths", "output path"],
        "output": "written local feature-matrix path",
        "source": "features.feature_matrix.build_feature_matrix",
        "side_effect": "local output write only; no remote fetch or Freqtrade CLI",
    },
    "create_crowding_fader_port": {
        "inputs": ["optional S2 parameters"],
        "output": "stateful BtcqtStrategyPort for btcqt_s2_crowding_fader",
        "source": "btcqt.strategy.s2_crowding_fader.S2CrowdingFader",
        "lifecycle": "worker must retain the port and send actual fill callbacks to port.on_fill",
    },
    "decide_crowding_fader": {
        "inputs": ["retained S2 port", "CausalStateTimeline", "timestamp", "equity"],
        "output": "PortDecision containing true order intents/rejections",
        "source": "btcqt S2 on_state via strategy port",
    },
    "build_source_research_report": {
        "inputs": ["evolution results", "walk-forward results", "catalog results"],
        "output": "source identities plus source-derived ranking/evaluation payload",
        "side_effect": "none",
    },
}


def evolution_candidates() -> tuple[EvolutionCandidate, ...]:
    """Return the exact deterministic source seed space."""
    return tuple(default_candidate_space())


def rank_evolution_candidates(results: list[dict[str, object]]) -> list[dict[str, object]]:
    """Rank train/validation evidence only through the source scorer."""
    return rank_evolution_results(results)


def evaluate_promotion(
    candidate_id: str,
    reports: dict[str, dict[str, object]],
    policy: PromotionPolicy = DEFAULT_PROMOTION_POLICY,
) -> dict[str, object]:
    """Apply the source final-OOS promotion gate without any execution side effect."""
    return evaluate_candidate_promotion(candidate_id, reports, policy)


def evaluate_acceptance(
    payload: dict[str, Any],
    *,
    root_dir: Path,
    policy: CandidateAcceptancePolicy = DEFAULT_ACCEPTANCE_POLICY,
) -> dict[str, Any]:
    """Apply the source rolling split/parameter/bootstrap acceptance gate."""
    return evaluate_candidate_acceptance(payload, root_dir=root_dir, policy=policy)


def build_evolution_plan(*, start: datetime, end: datetime) -> tuple[EvolutionSplit, ...]:
    """Build the source train/validate/final-OOS split plan from calendar dates."""
    return build_evolution_split_plan(start.date(), end.date())


def default_evolution_plan() -> tuple[EvolutionSplit, ...]:
    """Return the original explicit pre-2026 plan; this does not execute it."""
    return tuple(default_pre_2026_splits())


def build_walk_forward_plan(request: WalkForwardRequest) -> tuple[dict[str, Any], ...]:
    """Return source split request dictionaries; no subprocesses or research runs."""
    return tuple(split.as_dict() for split in build_walk_forward_splits(request))


def evaluate_walk_forward_results(split_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate existing split artifacts through the source walk-forward gate."""
    return evaluate_walk_forward(split_results)


def evaluate_feature_matrix_quality(
    frame: pd.DataFrame,
    policy: FeatureQualityPolicy,
) -> dict[str, object]:
    """Run the source anti-lookahead/gap/staleness quality check on in-memory data."""
    return evaluate_feature_quality(frame.copy(deep=True), policy)


def validate_feature_matrix_schema(frame: pd.DataFrame) -> dict[str, object]:
    """Validate the source strategy schema and return a worker-friendly report."""
    validate_strategy_schema(frame.copy(deep=True))
    return {"passed": True, "row_count": len(frame)}


def align_features_asof(
    candles: pd.DataFrame,
    features: pd.DataFrame,
    tolerance: pd.Timedelta | None = None,
) -> pd.DataFrame:
    """Delegate causal feature alignment to the original evolution implementation."""
    return align_asof_features(candles.copy(deep=True), features.copy(deep=True), tolerance)


def evaluate_ohlcv_data_quality(
    *,
    coverages: list[dict[str, Any]],
    pair: str,
    timeframe: str,
    policy: DataQualityPolicy,
    as_of: datetime,
) -> dict[str, Any]:
    """Run the pure source coverage/staleness gate; never invoke Freqtrade CLI."""
    return evaluate_data_quality(
        coverages=coverages,
        pair=pair,
        timeframe=timeframe,
        policy=policy,
        as_of=as_of,
    )


def evaluate_ohlcv_integrity_gate(
    *, rows: list[dict[str, Any]], timeframe: str
) -> dict[str, Any]:
    """Run the source pure OHLCV duplicate/gap/invalid-candle gate."""
    return evaluate_ohlcv_integrity(rows=rows, timeframe=timeframe)


def evaluate_trade_robustness_gate(
    *, trades: list[dict[str, Any]], policy: RobustnessPolicy
) -> dict[str, Any]:
    """Run source cost-stress and loss-cluster evidence on completed trades."""
    return evaluate_trade_robustness(trades=trades, policy=policy)


def evaluate_regime_robustness_gate(
    *,
    candles: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    policy: RegimePolicy,
) -> dict[str, Any]:
    """Run source causal regime classification against completed trades."""
    return evaluate_regime_robustness(candles=candles, trades=trades, policy=policy)


def evaluate_config_risk_gate(
    *, config: dict[str, Any], policy: RiskPolicy | None = None
) -> dict[str, Any]:
    """Run the source config risk gate without loading a Freqtrade runtime."""
    return evaluate_config_risk(config, policy)


def evaluate_orderbook_liquidity_gate(
    *, snapshot: dict[str, Any], policy: LiquidityPolicy
) -> dict[str, Any]:
    """Evaluate a supplied snapshot only; this adapter never fetches order books."""
    return evaluate_orderbook_liquidity(snapshot=snapshot, policy=policy)


def evaluate_dry_run_evidence_gate(
    *,
    config: dict[str, Any],
    log_text: str,
    min_duration_days: int,
    launch_started_at: datetime | None = None,
    launch_completed_at: datetime | None = None,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """Run source dry-run checks over caller-provided config/log evidence only."""
    return evaluate_dry_run_evidence(
        config=config,
        log_text=log_text,
        min_duration_days=min_duration_days,
        launch_started_at=launch_started_at,
        launch_completed_at=launch_completed_at,
        evaluated_at=evaluated_at,
    )


def evaluate_readiness_json_gate(
    *, payload: dict[str, Any], key_path: tuple[str, ...], source_name: str
) -> dict[str, bool | str | None]:
    """Apply original readiness semantics to an already parsed artifact payload."""
    return evaluate_json_gate(payload, key_path=key_path, source_name=source_name)


def rank_catalog_backtests(results: list[dict[str, object]]) -> list[dict[str, object]]:
    """Rank completed source catalog backtest artifacts via the original comparator."""
    return rank_catalog_results(results)


def build_feature_matrix_from_local_artifacts(
    *,
    root_dir: Path,
    exchange: str,
    pair: str,
    timeframe: str,
    external_paths: Sequence[Path],
    output: Path,
    start: pd.Timestamp | str | None = None,
    end: pd.Timestamp | str | None = None,
) -> Path:
    """Explicit local-file wrapper for the source builder; it performs no remote calls."""
    return build_feature_matrix(
        root_dir=root_dir,
        exchange=exchange,
        pair=pair,
        timeframe=timeframe,
        external_paths=external_paths,
        output=output,
        start=start,
        end=end,
    )


def create_crowding_fader_port(
    parameters: Mapping[str, object] | None = None,
) -> BtcqtStrategyPort:
    """Create the actual stateful btcqt S2 port; default remains source-disabled."""
    return create_btcqt_port("btcqt_s2_crowding_fader", parameters)


def decide_crowding_fader(
    port: BtcqtStrategyPort,
    timeline: CausalStateTimeline,
    *,
    timestamp: datetime,
    equity: float,
) -> PortDecision:
    """Run one exact S2 state transition; caller retains port and fill lifecycle."""
    return port.decide(timeline, timestamp=timestamp, equity=equity)


def build_source_research_report(
    *,
    evolution_results: list[dict[str, object]],
    walk_forward_results: list[dict[str, Any]],
    catalog_results: list[dict[str, object]],
) -> dict[str, object]:
    """Pure API payload with source identities and source-derived comparisons."""
    return {
        "sources": {
            "btcqt": asdict(BTCQT_SOURCE),
            "btc_quant_evolution": asdict(EVOLUTION_SOURCE),
        },
        "evolution": {
            "candidate_ids": [candidate.id for candidate in evolution_candidates()],
            "ranking": [str(result.get("candidate_id")) for result in rank_evolution_candidates(evolution_results)],
        },
        "walk_forward": evaluate_walk_forward_results(walk_forward_results),
        "catalog": rank_catalog_backtests(catalog_results),
    }


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
    "align_features_asof",
    "build_evolution_plan",
    "build_feature_matrix_from_local_artifacts",
    "build_source_research_report",
    "build_walk_forward_plan",
    "create_crowding_fader_port",
    "decide_crowding_fader",
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
    "rank_catalog_backtests",
    "rank_evolution_candidates",
    "validate_feature_matrix_schema",
]
