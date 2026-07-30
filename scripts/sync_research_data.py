"""Refresh the managed ten-year Bitstamp research dataset."""

from __future__ import annotations

import argparse
from pathlib import Path

from qt.research.datasets import DatasetSynchronizer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parquet-dir", default="data/parquet")
    args = parser.parse_args()
    dataset = DatasetSynchronizer(Path(args.parquet_dir)).sync(
        "bitstamp-btcusd-1d-10y"
    )
    print(
        f"synced {dataset['dataset_id']}: "
        f"{dataset['rows']} rows, {dataset['status']}"
    )


if __name__ == "__main__":
    main()
