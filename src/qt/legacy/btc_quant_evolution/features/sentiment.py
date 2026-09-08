# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import pandas as pd


def derive_sentiment_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Add deterministic sentiment and news regime features."""
    result = frame.copy()
    if "fear_greed_value" in result:
        result["fear_greed_change"] = result["fear_greed_value"].diff()
        result["fear_greed_extreme"] = (result["fear_greed_value"] <= 20) | (result["fear_greed_value"] >= 80)
    if "article_count" in result:
        result["article_count_change"] = result["article_count"].pct_change()
    return result

