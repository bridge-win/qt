# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from qt.legacy.btc_quant_evolution.evolution.candidate_space import EvolutionCandidate
from qt.legacy.btc_quant_evolution.research_pipeline import BacktestRequest


class EvolutionSplitLike(Protocol):
    name: str
    timerange: str


def build_evolution_request(
    *,
    candidate: EvolutionCandidate,
    split: EvolutionSplitLike,
    feature_matrix: Path,
    candidate_config: Path,
    run_id: str,
) -> BacktestRequest:
    """Build the fixed BTC spot, 4h request for one candidate and split."""
    return BacktestRequest(
        exchange="binance",
        pair="BTC/USDT",
        timeframe="4h",
        days=None,
        timerange=split.timerange,
        strategy="BtcMultiSourceRegimeStrategy",
        run_id=f"{run_id}-{candidate.id}-{split.name}",
        include_bias_checks=split.name == "final_oos",
        exchange_check=False,
        feature_matrix=feature_matrix.as_posix(),
        candidate_config=candidate_config.as_posix(),
    )


def write_candidate_config(
    *,
    root_dir: Path,
    run_id: str,
    candidate: EvolutionCandidate,
) -> Path:
    """Write the local strategy configuration used for every candidate split."""
    config_path = root_dir / "user_data" / "research_runs" / run_id / "candidates" / f"{candidate.id}.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(asdict(candidate), indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    return config_path.relative_to(root_dir)

