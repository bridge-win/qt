from __future__ import annotations

from pathlib import Path

from qt.backtest.strategy_backtest import synthetic_btc_ohlcv
from qt.data.store import ParquetStore
from qt.research.datasets import DatasetCatalog


def test_dataset_catalog_reports_truthful_managed_states(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    store.write("ohlcv", "okx_BTCUSDT_1h", synthetic_btc_ohlcv(days=45))
    store.write(
        "ohlcv",
        "binance_BTCUSDT_1h",
        synthetic_btc_ohlcv(days=1).head(0),
    )

    datasets = {
        item["dataset_id"]: item
        for item in DatasetCatalog(tmp_path).list_datasets()
    }

    assert datasets["bitstamp-btcusd-1d-10y"]["status"] == "missing"
    assert datasets["bitstamp-btcusd-1d-10y"]["standard_ready"] is False
    assert datasets["okx-btcusdt-1h"]["status"] == "ready"
    assert datasets["okx-btcusdt-1h"]["rows"] > 0
    assert datasets["okx-btcusdt-1h"]["symbol"] == "BTC/USDT"
    assert "binance-btcusdt-1h" not in datasets
