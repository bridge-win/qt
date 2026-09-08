# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import pandas as pd


def derive_onchain_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Add change features for locally materialized on-chain metrics."""
    result = frame.copy()
    for column in ("active_addresses", "transaction_count", "hash_rate"):
        if column in result:
            result[f"{column}_change"] = result[column].pct_change()
    return result

