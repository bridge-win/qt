# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from qt.legacy.btc_quant_evolution.features.align import align_asof_features
from qt.legacy.btc_quant_evolution.features.derivatives import derive_derivatives_features
from qt.legacy.btc_quant_evolution.features.onchain import derive_onchain_features
from qt.legacy.btc_quant_evolution.features.orderflow import derive_orderflow_features
from qt.legacy.btc_quant_evolution.features.quality import FeatureQualityPolicy, evaluate_feature_quality
from qt.legacy.btc_quant_evolution.features.sentiment import derive_sentiment_features
from qt.legacy.btc_quant_evolution.ohlcv_integrity import data_file_path

_METADATA_COLUMNS = {"timestamp", "available_at", "source", "dataset", "symbol"}
STRATEGY_FEATURE_COLUMNS = (
    "technical_score",
    "derivatives_score",
    "onchain_score",
    "sentiment_score",
    "liquidity_score",
    "liquidity_bad",
    "leverage_crowded",
)
_CANONICAL_ALIASES = {
    "funding_rate": ("funding_rate",),
    "open_interest": ("sumOpenInterest", "open_interest"),
    "long_short_ratio": ("longShortRatio", "long_short_ratio"),
    "taker_buy_volume": ("buyVol", "taker_buy_volume"),
    "taker_sell_volume": ("sellVol", "taker_sell_volume"),
    "active_addresses": ("active_addresses",),
    "transaction_count": ("transaction_count",),
    "hash_rate": ("hash_rate",),
    "fear_greed_value": ("fear_greed_value",),
    "article_count": ("article_count",),
    "tone": ("tone",),
    "best_bid": ("best_bid",),
    "best_ask": ("best_ask",),
    "bid_depth": ("bid_depth",),
    "ask_depth": ("ask_depth",),
}
DEFAULT_FEATURE_QUALITY_POLICY = FeatureQualityPolicy(max_gap_fraction=0.05, max_staleness_hours=48)
_PARTIAL_HISTORY_PREFIXES = frozenset({"binance_futures_funding_rate"})


def build_feature_matrix(
    root_dir: Path,
    exchange: str,
    pair: str,
    timeframe: str,
    external_paths: Sequence[Path],
    output: Path,
    start: pd.Timestamp | str | None = None,
    end: pd.Timestamp | str | None = None,
) -> Path:
    """Build a local, immutable feature matrix without provider network calls."""
    if not external_paths:
        raise ValueError("at least one external parquet is required")
    output_path = output if output.is_absolute() else root_dir / output
    if output_path.exists():
        raise FileExistsError(f"feature matrix already exists: {output_path}")

    ohlcv_path = data_file_path(root_dir=root_dir, exchange=exchange, pair=pair, timeframe=timeframe)
    candles = pd.read_feather(ohlcv_path)
    if "date" not in candles.columns:
        raise ValueError("OHLCV data must include date")
    matrix = candles.copy()
    matrix["date"] = pd.to_datetime(matrix["date"], utc=True, errors="raise")
    if (start is None) != (end is None):
        raise ValueError("feature matrix start and end must be provided together")
    if start is not None and end is not None:
        effective_start = pd.Timestamp(start)
        effective_end = pd.Timestamp(end)
        effective_start = (
            effective_start.tz_localize("UTC")
            if effective_start.tzinfo is None
            else effective_start.tz_convert("UTC")
        )
        effective_end = (
            effective_end.tz_localize("UTC")
            if effective_end.tzinfo is None
            else effective_end.tz_convert("UTC")
        )
        if effective_start >= effective_end:
            raise ValueError("feature matrix start must be before end")
        matrix = matrix.loc[
            (matrix["date"] >= effective_start) & (matrix["date"] < effective_end)
        ].copy()
        if matrix.empty:
            raise ValueError("feature matrix effective window contains no OHLCV rows")

    availability_columns: list[str] = []
    for external_path in external_paths:
        feature_frame = _read_external_parquet(external_path)
        prefix = _artifact_prefix(external_path, feature_frame)
        renamed = _rename_feature_columns(feature_frame, prefix)
        available_column = f"{prefix}_available_at"
        renamed = renamed.rename(columns={"available_at": available_column})
        aligned = align_asof_features(
            matrix,
            renamed.rename(columns={available_column: "available_at"}),
            tolerance=None,
        ).rename(columns={"available_at": available_column})
        value_columns = [
            column
            for column in renamed.columns
            if column != available_column and not column.endswith(("_source", "_dataset"))
        ]
        _validate_artifact_quality(aligned, prefix, available_column, value_columns)
        matrix = aligned
        availability_columns.append(available_column)

    matrix["available_at"] = matrix[availability_columns].max(axis=1)
    matrix["feature_sources"] = matrix[availability_columns].apply(
        lambda row: ",".join(
            sorted(_source_prefix(column) for column in availability_columns if pd.notna(row[column]))
        ),
        axis=1,
    )
    matrix = _derive_strategy_features(matrix)
    validate_strategy_schema(matrix)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    try:
        matrix.to_parquet(temporary_path, index=False)
        temporary_path.replace(output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return output_path


def _read_external_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"external parquet does not exist: {path}")
    frame = pd.read_parquet(path)
    required = {"timestamp", "available_at", "source", "dataset"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"external parquet missing required columns: {', '.join(missing)}")
    return frame


def _artifact_prefix(path: Path, frame: pd.DataFrame) -> str:
    if frame.empty:
        return path.stem.replace("-", "_")
    sources = frame["source"].dropna().astype(str).unique()
    datasets = frame["dataset"].dropna().astype(str).unique()
    if len(sources) != 1 or len(datasets) != 1:
        raise ValueError(f"external parquet must contain exactly one source and dataset: {path}")
    source = sources[0]
    dataset = datasets[0]
    return f"{source}_{dataset}".replace("-", "_")


def _rename_feature_columns(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
    renamed = frame.drop(columns=["timestamp", "symbol"], errors="ignore").copy()
    values = sorted(column for column in renamed.columns if column not in _METADATA_COLUMNS)
    rename_map = {column: f"{prefix}_{column}" for column in values}
    rename_map.update({"source": f"{prefix}_source", "dataset": f"{prefix}_dataset"})
    return renamed.rename(columns=rename_map)


def _validate_artifact_quality(
    aligned: pd.DataFrame,
    prefix: str,
    available_column: str,
    value_columns: list[str],
) -> None:
    if not value_columns:
        raise ValueError(f"feature matrix quality failed for {prefix}: no feature value columns")

    active = aligned
    if prefix in _PARTIAL_HISTORY_PREFIXES:
        available = aligned[available_column].notna()
        if not available.any():
            raise ValueError(f"feature matrix quality failed for {prefix}: no available observations")
        active = aligned.loc[available[available].index[0] :]
    quality_frame = active[["date", available_column]].rename(columns={available_column: "available_at"})
    quality = evaluate_feature_quality(quality_frame, DEFAULT_FEATURE_QUALITY_POLICY)
    failed_reasons = list(quality["failed_reasons"])
    if active[value_columns].isna().any(axis=None):
        failed_reasons.append("feature value coverage is incomplete")
    if failed_reasons:
        raise ValueError(f"feature matrix quality failed for {prefix}: {failed_reasons}")


def validate_strategy_schema(matrix: pd.DataFrame) -> None:
    required = {"date", "available_at", "feature_sources", *STRATEGY_FEATURE_COLUMNS}
    missing = sorted(required - set(matrix.columns))
    if missing:
        raise ValueError(f"feature matrix missing strategy columns: {', '.join(missing)}")
    if matrix[list(STRATEGY_FEATURE_COLUMNS)].isna().any(axis=None):
        raise ValueError("feature matrix strategy columns contain missing values")
    availability = pd.to_datetime(matrix["available_at"], utc=True, errors="coerce")
    dates = pd.to_datetime(matrix["date"], utc=True, errors="coerce")
    if availability.isna().any() or dates.isna().any() or (availability > dates).any():
        raise ValueError("feature matrix violates availability contract")
    if matrix["feature_sources"].astype(str).str.strip().eq("").any():
        raise ValueError("feature matrix provenance is empty")


def _derive_strategy_features(matrix: pd.DataFrame) -> pd.DataFrame:
    result = matrix.copy()
    for canonical, aliases in _CANONICAL_ALIASES.items():
        source_column = _find_alias_column(result, aliases)
        if source_column is not None:
            result[canonical] = pd.to_numeric(result[source_column], errors="coerce")

    result = derive_orderflow_features(result)
    result = derive_derivatives_features(result)
    result = derive_onchain_features(result)
    result = derive_sentiment_features(result)

    momentum = result["close"].pct_change(6) * 10
    trend = (result["close"] / result["close"].rolling(12, min_periods=2).mean() - 1) * 10
    result["technical_score"] = (momentum + trend).clip(-1.0, 1.0).fillna(0.0)

    result["derivatives_score"] = _mean_score(
        result,
        {
            "funding_rate_zscore": -1 / 3,
            "open_interest_change": 10,
            "taker_buy_sell_imbalance": 1,
        },
    )
    result["onchain_score"] = _mean_score(
        result,
        {
            "active_addresses_change": 10,
            "transaction_count_change": 10,
            "hash_rate_change": 10,
        },
    )
    sentiment_components: list[pd.Series] = []
    if "fear_greed_value" in result:
        sentiment_components.append((result["fear_greed_value"] - 50) / 50)
    if "fear_greed_change" in result:
        sentiment_components.append(result["fear_greed_change"] / 20)
    if "tone" in result:
        sentiment_components.append(result["tone"] / 10)
    result["sentiment_score"] = _mean_components(sentiment_components, result.index)

    liquidity_components: list[pd.Series] = []
    volume_baseline = result["volume"].rolling(12, min_periods=2).median()
    liquidity_components.append((result["volume"] / volume_baseline - 1) * 2)
    if "spread_bps" in result:
        liquidity_components.append(-result["spread_bps"] / 50)
    if "depth_imbalance" in result:
        liquidity_components.append(result["depth_imbalance"])
    result["liquidity_score"] = _mean_components(liquidity_components, result.index)

    leverage_crowded = pd.Series(False, index=result.index)
    if "long_short_ratio" in result:
        leverage_crowded |= result["long_short_ratio"].ge(1.5).fillna(False)
    if "funding_rate_zscore" in result:
        leverage_crowded |= result["funding_rate_zscore"].ge(2.0).fillna(False)
    result["leverage_crowded"] = leverage_crowded.astype(bool)

    liquidity_bad = result["volume"].le(0) | result["volume"].isna()
    if "spread_bps" in result:
        liquidity_bad |= result["spread_bps"].gt(50).fillna(True)
    result["liquidity_bad"] = liquidity_bad.astype(bool)
    return result


def _find_alias_column(frame: pd.DataFrame, aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        candidates = sorted(column for column in frame.columns if column == alias or column.endswith(f"_{alias}"))
        if candidates:
            return candidates[0]
    return None


def _mean_score(frame: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    components = [frame[column] * weight for column, weight in weights.items() if column in frame]
    return _mean_components(components, frame.index)


def _mean_components(components: list[pd.Series], index: pd.Index) -> pd.Series:
    if not components:
        # Missing evidence is not a neutral vote; schema validation must fail closed.
        return pd.Series(float("nan"), index=index, dtype="float64")
    return pd.concat(components, axis=1).mean(axis=1).clip(-1.0, 1.0).fillna(0.0)


def _source_prefix(availability_column: str) -> str:
    return availability_column.removesuffix("_available_at")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an immutable local multi-source feature matrix.")
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbol", "--pair", dest="pair", default="BTC/USDT")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--external-path", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = build_feature_matrix(
        root_dir=args.root_dir.resolve(),
        exchange=args.exchange,
        pair=args.pair,
        timeframe=args.timeframe,
        external_paths=args.external_path,
        output=args.output,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

