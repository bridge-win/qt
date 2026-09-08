from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pandas.testing as pdt
import pytest

from qt.legacy.btc_quant_evolution.candidate_acceptance import evaluate_candidate_acceptance
from qt.legacy.btc_quant_evolution.catalog_backtest import rank_catalog_results
from qt.legacy.btc_quant_evolution.data_quality import evaluate_data_quality
from qt.legacy.btc_quant_evolution.dry_run_evidence import evaluate_dry_run_evidence
from qt.legacy.btc_quant_evolution.evolution.candidate_space import default_candidate_space
from qt.legacy.btc_quant_evolution.evolution.promotion_gate import evaluate_candidate_promotion
from qt.legacy.btc_quant_evolution.evolution.scorer import rank_evolution_results
from qt.legacy.btc_quant_evolution.evolution.walk_forward_evolution import (
    build_evolution_split_plan,
)
from qt.legacy.btc_quant_evolution.features.align import align_asof_features
from qt.legacy.btc_quant_evolution.features.feature_matrix import validate_strategy_schema
from qt.legacy.btc_quant_evolution.features.quality import evaluate_feature_quality
from qt.legacy.btc_quant_evolution.liquidity_evidence import evaluate_orderbook_liquidity
from qt.legacy.btc_quant_evolution.ohlcv_integrity import evaluate_ohlcv_integrity
from qt.legacy.btc_quant_evolution.readiness import evaluate_json_gate
from qt.legacy.btc_quant_evolution.regime import evaluate_regime_robustness
from qt.legacy.btc_quant_evolution.risk_evidence import evaluate_config_risk
from qt.legacy.btc_quant_evolution.robustness import evaluate_trade_robustness
from qt.legacy.btc_quant_evolution.walk_forward import (
    WalkForwardRequest,
    build_walk_forward_splits,
    evaluate_walk_forward,
)
from qt.legacy.btcqt.config import S2Cfg
from qt.legacy.btcqt.models import MarketState, Regime, Side
from qt.legacy.btcqt.strategy.s2_crowding_fader import S2CrowdingFader
from qt.strategy_ports.btcqt import CausalState, CausalStateTimeline, DataVersion
from qt.workbench import fusion
from qt.workbench.source_research import (
    DataQualityPolicy,
    FeatureQualityPolicy,
    LiquidityPolicy,
    PromotionPolicy,
    RegimePolicy,
    RiskPolicy,
    RobustnessPolicy,
    align_features_asof,
    build_evolution_plan,
    build_source_research_report,
    build_walk_forward_plan,
    create_crowding_fader_port,
    decide_crowding_fader,
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

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _state(at: datetime, **changes: object) -> MarketState:
    state = MarketState(
        ts=int(at.timestamp() * 1000),
        price=30_000.0,
        ema_anchor=30_100.0,
        atr=60.0,
        atr_tf=600.0,
        sigma_1m=0.0007,
        vel5=-0.2,
        dev=1.0,
        funding_pctl=99.0,
        open_interest_pctl=90.0,
        regime=Regime.RANGE,
        warmed_up=True,
    )
    for key, value in changes.items():
        setattr(state, key, value)
    return state


def _causal(state: MarketState) -> CausalState:
    timestamp = datetime.fromtimestamp(state.ts / 1000, tz=timezone.utc)
    return CausalState(
        state=state,
        observed_at=timestamp,
        available_at=timestamp,
        inputs=(
            DataVersion("ohlcv_1m", "fixture", timestamp),
            DataVersion("funding_settlements", "fixture", timestamp),
            DataVersion("open_interest", "fixture", timestamp),
        ),
    )


def _signature(items: Sequence[Any]) -> list[tuple[object, ...]]:
    return [
        (item.action, item.strategy, item.side, item.price, item.qty, item.reason)
        for item in items
    ]


def test_candidate_space_and_selection_ranking_delegate_to_original_evolution() -> None:
    assert evolution_candidates() == tuple(default_candidate_space())
    results: list[dict[str, object]] = [
        {"candidate_id": "b", "selection": {"passed": True, "metrics": {"calmar": 2.0}}},
        {"candidate_id": "a", "selection": {"passed": True, "metrics": {"calmar": 3.0}}},
        {"candidate_id": "c", "selection": {"passed": False, "metrics": {"calmar": 100.0}}},
    ]
    assert rank_evolution_candidates(results) == rank_evolution_results(results)
    assert fusion.default_candidate_space() == tuple(default_candidate_space())
    assert fusion.rank_candidates(results) == rank_evolution_results(results)


def test_promotion_walk_forward_data_and_feature_gates_match_source(tmp_path: Path) -> None:
    reports = {
        "final_oos": {
            "summary": {
                "profit_total_pct": 1.0,
                "profit_factor": 1.3,
                "max_drawdown_pct": 10.0,
                "total_trades": 60,
                "sortino": 0.6,
            },
            "bias": {"lookahead_passed": True, "recursive_passed": True},
            "walk_forward": {"passed": True},
            "robustness": {"passed": True},
            "regime": {"passed": True},
            "dry_run": {"passed": True},
            "operational": {"passed": True},
        }
    }
    policy = PromotionPolicy()
    assert evaluate_promotion("candidate", reports, policy) == evaluate_candidate_promotion("candidate", reports, policy)
    acceptance_payload: dict[str, Any] = {"evaluation": {"aggregate": {}}, "splits": []}
    assert evaluate_acceptance(acceptance_payload, root_dir=tmp_path) == evaluate_candidate_acceptance(
        acceptance_payload,
        root_dir=tmp_path,
    )

    request = WalkForwardRequest(
        exchange="binance",
        pair="BTC/USDT",
        timeframe="4h",
        strategy="BtcMultiSourceRegimeStrategy",
        start=T0.date(),
        end=(T0 + timedelta(days=800)).date(),
        train_days=365,
        test_days=90,
        step_days=90,
    )
    assert build_walk_forward_plan(request) == tuple(split.as_dict() for split in build_walk_forward_splits(request))
    split_results: list[dict[str, object]] = []
    assert evaluate_walk_forward_results(split_results) == evaluate_walk_forward(split_results)

    quality_frame = pd.DataFrame(
        {"date": [T0], "available_at": [T0 - timedelta(hours=1)]}
    )
    quality_policy = FeatureQualityPolicy(max_gap_fraction=0.05, max_staleness_hours=48)
    assert evaluate_feature_matrix_quality(quality_frame, quality_policy) == evaluate_feature_quality(
        quality_frame, quality_policy
    )
    coverage = [{"pair": "BTC/USDT", "timeframe": "4h", "candles": 100, "end": T0.isoformat()}]
    data_policy = DataQualityPolicy(min_candles=50, max_staleness_hours=48)
    assert evaluate_ohlcv_data_quality(
        coverages=coverage, pair="BTC/USDT", timeframe="4h", policy=data_policy, as_of=T0
    ) == evaluate_data_quality(
        coverages=coverage, pair="BTC/USDT", timeframe="4h", policy=data_policy, as_of=T0
    )


def test_source_feature_alignment_is_causal_and_does_not_mutate_inputs() -> None:
    candles = pd.DataFrame({"date": [T0, T0 + timedelta(hours=4)]})
    features = pd.DataFrame(
        {
            "timestamp": [T0, T0 + timedelta(hours=4)],
            "available_at": [T0, T0 + timedelta(hours=5)],
            "score": [1.0, 99.0],
        }
    )
    actual = align_features_asof(candles, features)
    expected = align_asof_features(candles, features, None)
    pdt.assert_frame_equal(actual, expected)
    assert actual["score"].iloc[-1] == pytest.approx(1.0)
    features.loc[features.index[-1], "score"] = -1.0
    assert actual["score"].iloc[0] == pytest.approx(1.0)


def test_catalog_comparison_evolution_plan_and_schema_delegate_to_sources() -> None:
    results: list[dict[str, object]] = [
        {
            "profile": "source-b",
            "status": "completed",
            "readiness": {"passed": True},
            "summary": {"calmar": 1.5, "profit_factor": 1.2, "sortino": 0.5, "total_trades": 60},
        },
        {
            "profile": "source-a",
            "status": "completed",
            "readiness": {"passed": True},
            "summary": {"calmar": 2.0, "profit_factor": 1.2, "sortino": 0.5, "total_trades": 60},
        },
    ]
    assert rank_catalog_backtests(results) == rank_catalog_results(results)
    assert build_evolution_plan(start=T0, end=T0 + timedelta(days=1_500)) == build_evolution_split_plan(
        T0.date(),
        (T0 + timedelta(days=1_500)).date(),
    )

    schema_frame = pd.DataFrame(
        {
            "date": [T0],
            "available_at": [T0],
            "feature_sources": ["fixture"],
            "technical_score": [0.0],
            "derivatives_score": [0.0],
            "onchain_score": [0.0],
            "sentiment_score": [0.0],
            "liquidity_score": [0.0],
            "liquidity_bad": [False],
            "leverage_crowded": [False],
        }
    )
    validate_strategy_schema(schema_frame)
    assert validate_feature_matrix_schema(schema_frame) == {"passed": True, "row_count": 1}


def test_remaining_research_gates_delegate_without_runtime_or_network() -> None:
    trades: list[dict[str, Any]] = []
    candles: list[dict[str, Any]] = []
    assert evaluate_trade_robustness_gate(trades=trades, policy=RobustnessPolicy()) == evaluate_trade_robustness(
        trades=trades,
        policy=RobustnessPolicy(),
    )
    assert evaluate_regime_robustness_gate(
        candles=candles,
        trades=trades,
        policy=RegimePolicy(),
    ) == evaluate_regime_robustness(candles=candles, trades=trades, policy=RegimePolicy())
    assert evaluate_ohlcv_integrity_gate(rows=[], timeframe="4h") == evaluate_ohlcv_integrity(
        rows=[],
        timeframe="4h",
    )
    config: dict[str, Any] = {}
    assert evaluate_config_risk_gate(config=config, policy=RiskPolicy()) == evaluate_config_risk(
        config,
        RiskPolicy(),
    )
    snapshot: dict[str, Any] = {}
    liquidity_policy = LiquidityPolicy(order_notional=1_000)
    assert evaluate_orderbook_liquidity_gate(
        snapshot=snapshot,
        policy=liquidity_policy,
    ) == evaluate_orderbook_liquidity(snapshot=snapshot, policy=liquidity_policy)
    assert evaluate_dry_run_evidence_gate(
        config={"dry_run": False},
        log_text="",
        min_duration_days=21,
    ) == evaluate_dry_run_evidence(config={"dry_run": False}, log_text="", min_duration_days=21)
    payload: dict[str, Any] = {"production_gate": {"passed": True}}
    assert evaluate_readiness_json_gate(
        payload=payload,
        key_path=("production_gate",),
        source_name="summary.json",
    ) == evaluate_json_gate(
        payload,
        key_path=("production_gate",),
        source_name="summary.json",
    )


def test_crowding_fader_uses_exact_stateful_btcqt_port_not_a_factor_proxy() -> None:
    states = [_causal(_state(T0)), _causal(_state(T0 + timedelta(hours=8)))]
    timeline = CausalStateTimeline(states)
    port = create_crowding_fader_port({"enabled": True})
    direct = S2CrowdingFader(S2Cfg(enabled=True))
    first = decide_crowding_fader(port, timeline, timestamp=T0, equity=10_000.0)
    direct_first = direct.on_state(states[0].state, 10_000.0)
    second_at = T0 + timedelta(hours=8)
    second = decide_crowding_fader(port, timeline, timestamp=second_at, equity=10_000.0)
    direct_second = direct.on_state(states[1].state, 10_000.0)
    assert _signature(first.intents) == _signature(direct_first)
    assert _signature(second.intents) == _signature(direct_second)
    assert second.intents[0].side is Side.SELL


def test_report_builder_is_pure_source_derived_api_payload() -> None:
    report = build_source_research_report(evolution_results=[], walk_forward_results=[], catalog_results=[])
    sources = report["sources"]
    evolution = report["evolution"]
    assert isinstance(sources, dict)
    assert isinstance(evolution, dict)
    assert sources["btcqt"]["module"] == "qt.legacy.btcqt"
    assert sources["btc_quant_evolution"]["source_version"] == "multisource-evolution"
    assert evolution["candidate_ids"] == [candidate.id for candidate in default_candidate_space()]
    assert report["catalog"] == []
    assert fusion.RESEARCH_CONTRACT["align_features_asof"]["source"] == "features.align.align_asof_features"
    assert fusion.RESEARCH_CONTRACT["create_crowding_fader_port"]["lifecycle"] == (
        "worker must retain the port and send actual fill callbacks to port.on_fill"
    )
    plan = build_evolution_plan(start=T0, end=T0 + timedelta(days=1_500))
    assert [split.name for split in plan] == ["train", "validate", "final_oos"]
