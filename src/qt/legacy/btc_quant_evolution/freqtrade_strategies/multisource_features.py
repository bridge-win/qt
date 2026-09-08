# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
from pandas import DataFrame

REQUIRED_STRATEGY_COLUMNS = (
    "technical_score",
    "derivatives_score",
    "onchain_score",
    "sentiment_score",
    "liquidity_score",
    "liquidity_bad",
    "leverage_crowded",
)
REQUIRED_CONTRACT_COLUMNS = ("date", "available_at", "feature_sources", *REQUIRED_STRATEGY_COLUMNS)


def load_feature_matrix(path: Path) -> DataFrame:
    """Load a locally materialized feature matrix for strategy evaluation."""
    matrix = pd.read_parquet(path).copy()
    missing = sorted(set(REQUIRED_CONTRACT_COLUMNS) - set(matrix.columns))
    if missing:
        raise ValueError(f"multi-source feature matrix missing required columns: {', '.join(missing)}")

    matrix["date"] = pd.to_datetime(matrix["date"], utc=True, errors="raise")
    if matrix["date"].duplicated().any():
        raise ValueError("multi-source feature matrix contains duplicate dates")
    if matrix["feature_sources"].astype(str).str.strip().eq("").any():
        raise ValueError("multi-source feature matrix contains empty provenance")

    availability_columns = sorted(
        column for column in matrix.columns if column == "available_at" or column.endswith("_available_at")
    )
    source_availability_columns = [column for column in availability_columns if column != "available_at"]
    if not source_availability_columns:
        raise ValueError("multi-source feature matrix must include source availability columns")
    for column in source_availability_columns:
        prefix = column.removesuffix("_available_at")
        if f"{prefix}_source" not in matrix or f"{prefix}_dataset" not in matrix:
            raise ValueError(f"multi-source feature matrix missing provenance columns for: {prefix}")
    for column in availability_columns:
        matrix[column] = pd.to_datetime(matrix[column], utc=True, errors="raise")
        if column == "available_at" and matrix[column].isna().any():
            raise ValueError(f"multi-source feature matrix contains missing availability: {column}")
        observed = matrix[column].notna()
        if column != "available_at" and not observed.any():
            raise ValueError(f"multi-source feature matrix contains no availability: {column}")
        if (matrix.loc[observed, column] > matrix.loc[observed, "date"]).any():
            raise ValueError(f"multi-source feature matrix has availability timestamp after evaluation candle: {column}")

    for column in REQUIRED_STRATEGY_COLUMNS[:5]:
        values = pd.to_numeric(matrix[column], errors="coerce")
        if values.isna().any() or not values.map(math.isfinite).all():
            raise ValueError(f"multi-source feature matrix contains invalid score values: {column}")
        matrix[column] = values
    for column in REQUIRED_STRATEGY_COLUMNS[5:]:
        if matrix[column].isna().any() or not pd.api.types.is_bool_dtype(matrix[column]):
            raise ValueError(f"multi-source feature matrix contains invalid flag values: {column}")
    return matrix


def merge_feature_matrix(dataframe: DataFrame, feature_matrix: DataFrame) -> DataFrame:
    """Merge features only when their materialized candle timestamp matches exactly."""
    if "date" not in dataframe.columns:
        raise ValueError("candle dataframe must include date")

    candles = dataframe.copy()
    candles["date"] = pd.to_datetime(candles["date"], utc=True, errors="raise")
    feature_columns = [column for column in feature_matrix.columns if column == "date" or column not in candles]
    return candles.merge(feature_matrix.loc[:, feature_columns], on="date", how="left", validate="many_to_one")

