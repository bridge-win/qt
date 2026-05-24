from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from qt.data.catalog import data_source_statuses
from qt.data.store import ParquetStore


def test_data_source_status_reports_local_freshness(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    idx = pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
         "close": [1.0, 1.0], "volume": [1.0, 1.0]},
        index=idx,
    )
    store.write("ohlcv", "binance_BTCUSDT_1h", df)

    statuses = data_source_statuses(
        store,
        now=datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
    )
    ohlcv = next(row for row in statuses if row["id"] == "ohlcv_binance_btcusdt_1h")

    assert ohlcv["exists"] is True
    assert ohlcv["fresh"] is True
    assert ohlcv["rows"] == 2
    assert "Price action" in str(ohlcv["used_for"])
