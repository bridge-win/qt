from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest
from btc_backtest.data.models import DataRequest
from btc_backtest.data.providers.bitstamp import BitstampProvider
from btc_backtest.errors import (
    DataValidationError,
    NetworkUnavailableError,
    ProviderError,
)
from pytest_httpx import HTTPXMock

UTC = timezone.utc


def daily_request(
    start: str = "2024-01-01",
    end: str = "2024-01-04",
) -> DataRequest:
    return DataRequest(
        provider="bitstamp",
        market="spot",
        symbol="BTC/USD",
        timeframe="1d",
        start=pd.Timestamp(start, tz="UTC").to_pydatetime(),
        end=pd.Timestamp(end, tz="UTC").to_pydatetime(),
    )


def hourly_request() -> DataRequest:
    return DataRequest(
        provider="bitstamp",
        market="spot",
        symbol="BTC/USD",
        timeframe="1h",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 1, 3, tzinfo=UTC),
    )


def bitstamp_payload(
    *dates: str,
    pair: str = "BTC/USD",
    include_market: bool = True,
) -> dict[str, object]:
    data: dict[str, object] = {
        "pair": pair,
        "ohlc": [
            {
                "timestamp": str(
                    int(pd.Timestamp(date, tz="UTC").timestamp())
                ),
                "open": str(100 + index),
                "high": str(102 + index),
                "low": str(99 + index),
                "close": str(101 + index),
                "volume": str(10 + index),
            }
            for index, date in enumerate(dates)
        ],
    }
    if include_market:
        data["market"] = pair
    return {"data": data}


def test_bitstamp_paginates_closed_open_interval(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        json=bitstamp_payload("2024-01-01", "2024-01-02")
    )
    httpx_mock.add_response(json=bitstamp_payload("2024-01-03"))

    result = BitstampProvider(httpx.Client(), page_size=2).fetch(
        daily_request()
    )

    assert result.frame.index.tolist() == list(
        pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC")
    )
    assert len(result.manifest.raw_sha256) == 2
    assert result.manifest.real_data is True
    assert result.manifest.source == (
        "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
    )
    requests = httpx_mock.get_requests()
    assert [request.url.params["start"] for request in requests] == [
        str(int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())),
        str(int(datetime(2024, 1, 3, tzinfo=UTC).timestamp())),
    ]
    assert [request.url.params["limit"] for request in requests] == ["2", "1"]
    assert all("end" not in request.url.params for request in requests)
    assert all(
        request.url.params["exclude_current_candle"] == "true"
        for request in requests
    )


def test_bitstamp_maps_hourly_timeframe_to_3600_seconds(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        json=bitstamp_payload(
            "2024-01-01T00:00:00",
            "2024-01-01T01:00:00",
            "2024-01-01T02:00:00",
        )
    )

    result = BitstampProvider(httpx.Client()).fetch(hourly_request())

    assert result.frame.index.tolist() == list(
        pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    )
    request_params = httpx_mock.get_requests()[0].url.params
    assert request_params["step"] == "3600"
    assert request_params["limit"] == "3"


def test_bitstamp_rejects_cursor_stall(httpx_mock: HTTPXMock) -> None:
    page = bitstamp_payload("2024-01-01", "2024-01-02")
    httpx_mock.add_response(json=page)
    httpx_mock.add_response(json=page)

    with pytest.raises(ProviderError, match="cursor"):
        BitstampProvider(httpx.Client(), page_size=2).fetch(daily_request())


def test_bitstamp_retries_rate_limit_then_succeeds(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(status_code=429)
    httpx_mock.add_response(json=bitstamp_payload("2024-01-01"))

    result = BitstampProvider(
        httpx.Client(),
        page_size=2,
        retry_backoff=0.0,
    ).fetch(daily_request(end="2024-01-02"))

    assert len(result.frame) == 1
    assert len(httpx_mock.get_requests()) == 2


def test_bitstamp_bounds_transport_retries() -> None:
    attempts = 0

    def fail_transport(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(fail_transport)) as client,
        pytest.raises(NetworkUnavailableError, match="unreachable"),
    ):
        BitstampProvider(
            client,
            max_retries=2,
            retry_backoff=0.0,
        ).fetch(daily_request(end="2024-01-02"))

    assert attempts == 3


def test_bitstamp_accepts_live_shape_without_market_field(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        json=bitstamp_payload(
            "2024-01-01",
            include_market=False,
        )
    )

    result = BitstampProvider(httpx.Client()).fetch(
        daily_request(end="2024-01-02")
    )

    assert len(result.frame) == 1


def test_bitstamp_rejects_response_pair_mismatch(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        json=bitstamp_payload("2024-01-01", pair="BTC/USDT")
    )

    with pytest.raises(DataValidationError, match="pair"):
        BitstampProvider(httpx.Client()).fetch(
            daily_request(end="2024-01-02")
        )


@pytest.mark.parametrize(
    "data_request",
    [
        DataRequest(
            provider="bitstamp",
            market="futures",
            symbol="BTC/USD",
            timeframe="1d",
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 2, tzinfo=UTC),
        ),
        DataRequest(
            provider="bitstamp",
            market="spot",
            symbol="BTC/USDT",
            timeframe="1d",
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 2, tzinfo=UTC),
        ),
    ],
)
def test_bitstamp_rejects_unsupported_identity(
    data_request: DataRequest,
) -> None:
    with pytest.raises(ProviderError, match="support"):
        BitstampProvider(httpx.Client()).fetch(data_request)


def test_bitstamp_rejects_duplicate_page_timestamp(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        json=bitstamp_payload("2024-01-01", "2024-01-01")
    )

    with pytest.raises(DataValidationError, match="duplicate"):
        BitstampProvider(httpx.Client()).fetch(
            daily_request(end="2024-01-02")
        )
