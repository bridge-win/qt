from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest
from btc_backtest.data.models import DataRequest
from btc_backtest.data.providers.binance_archive import BinanceArchiveProvider
from btc_backtest.errors import NetworkUnavailableError

UTC = timezone.utc


@pytest.mark.integration
def test_binance_archive_live_verifies_one_monthly_file() -> None:
    data_request = DataRequest(
        provider="binance_archive",
        market="spot",
        symbol="BTC/USDT",
        timeframe="1d",
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 2, 1, tzinfo=UTC),
    )

    try:
        with httpx.Client(timeout=30.0) as client:
            result = BinanceArchiveProvider(client).fetch(data_request)
    except NetworkUnavailableError as error:
        pytest.skip(str(error))

    assert result.frame.index.tolist() == list(
        pd.date_range("2025-01-01", periods=31, freq="1D", tz="UTC")
    )
    assert result.manifest.real_data is True
    assert result.manifest.provider == "binance_archive"
    assert len(result.manifest.raw_sha256) == 1
