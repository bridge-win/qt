# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

from pathlib import Path


SOTA_RUNTIME_SOURCE_FILES = {
    "multisource_features": "multisource_features.py",
    "risk": "risk.py",
    "sota": "sota.py",
    "sota_indicators": "sota_indicators.py",
    "sota_params": "sota_params.py",
}


def sota_runtime_source_paths(root_dir: Path) -> dict[str, Path]:
    strategy_dir = root_dir / "user_data" / "strategies"
    return {
        name: strategy_dir / filename
        for name, filename in SOTA_RUNTIME_SOURCE_FILES.items()
    }

