from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest
from btc_backtest.data.models import DataRequest
from btc_backtest.data.providers.bitstamp import BitstampProvider
from btc_backtest.errors import NetworkUnavailableError

UTC = timezone.utc


@pytest.mark.integration
def test_bitstamp_live_returns_exact_historical_daily_window() -> None:
    data_request = DataRequest(
        provider="bitstamp",
        market="spot",
        symbol="BTC/USD",
        timeframe="1d",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 4, tzinfo=UTC),
    )

    try:
        with httpx.Client(timeout=15.0) as client:
            result = BitstampProvider(client).fetch(data_request)
    except NetworkUnavailableError as error:
        pytest.skip(str(error))

    assert result.frame.index.tolist() == list(
        pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC")
    )
    assert result.manifest.real_data is True
    assert result.manifest.provider == "bitstamp"
    assert result.manifest.raw_sha256
