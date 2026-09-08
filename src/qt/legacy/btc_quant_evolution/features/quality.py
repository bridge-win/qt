# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class FeatureQualityPolicy:
    max_gap_fraction: float
    max_staleness_hours: int

    def __post_init__(self) -> None:
        if not 0 <= self.max_gap_fraction <= 1:
            raise ValueError("max_gap_fraction must be between 0 and 1")
        if self.max_staleness_hours < 0:
            raise ValueError("max_staleness_hours must be non-negative")


def evaluate_feature_quality(frame: pd.DataFrame, policy: FeatureQualityPolicy) -> dict[str, object]:
    """Evaluate availability gaps, staleness, and anti-lookahead compliance."""
    row_count = len(frame)
    failed_reasons: list[str] = []
    if "date" not in frame.columns:
        failed_reasons.append("date column is required")
    if "available_at" not in frame.columns:
        failed_reasons.append("available_at column is required")
    if failed_reasons:
        return _result(failed_reasons, row_count, 1.0 if row_count else 0.0, None)

    dates = pd.to_datetime(frame["date"], utc=True, errors="coerce")
    available_at = pd.to_datetime(frame["available_at"], utc=True, errors="coerce")
    if dates.isna().any():
        failed_reasons.append("date column contains malformed timestamps")
    if available_at.isna().any():
        failed_reasons.append("available_at column contains malformed timestamps")

    valid = dates.notna() & available_at.notna()
    gap_fraction = float((~valid).mean()) if row_count else 0.0
    if gap_fraction > policy.max_gap_fraction:
        failed_reasons.append("feature gap fraction exceeds policy")

    staleness_hours: float | None = None
    if valid.any():
        staleness = (dates[valid] - available_at[valid]).dt.total_seconds() / 3600
        if (staleness < 0).any():
            failed_reasons.append("feature row is available after candle close")
        else:
            staleness_hours = float(staleness.max())
            if staleness_hours > policy.max_staleness_hours:
                failed_reasons.append("feature staleness exceeds policy")

    return _result(failed_reasons, row_count, gap_fraction, staleness_hours)


def _result(
    failed_reasons: list[str],
    row_count: int,
    gap_fraction: float,
    max_staleness_hours: float | None,
) -> dict[str, object]:
    return {
        "passed": not failed_reasons,
        "failed_reasons": failed_reasons,
        "row_count": row_count,
        "gap_fraction": gap_fraction,
        "max_staleness_hours": max_staleness_hours,
    }

