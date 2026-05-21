"""Local Parquet store. All ingested data lands here; backtests read only from here."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


class ParquetStore:
    """A simple per-dataset Parquet store, partitioned by dataset name.

    Layout::

        <root>/ohlcv/binance_BTCUSDT_1h.parquet
        <root>/funding/binance_BTCUSDT.parquet
        <root>/onchain/glassnode_mvrv_z.parquet
        ...

    Append-only semantics: writes merge by timestamp, dedup-last-wins.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, dataset: str, key: str) -> Path:
        d = self.root / dataset
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{key}.parquet"

    def read(self, dataset: str, key: str) -> pd.DataFrame:
        p = self.path(dataset, key)
        if not p.exists():
            return pd.DataFrame()
        return pd.read_parquet(p)

    def write(self, dataset: str, key: str, df: pd.DataFrame) -> Path:
        p = self.path(dataset, key)
        df.to_parquet(p, compression="zstd")
        return p

    def upsert(self, dataset: str, key: str, df: pd.DataFrame) -> Path:
        existing = self.read(dataset, key)
        if existing.empty:
            return self.write(dataset, key, df)
        merged = pd.concat([existing, df])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        return self.write(dataset, key, merged)
