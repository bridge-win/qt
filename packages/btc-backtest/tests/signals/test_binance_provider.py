from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from btc_backtest.errors import NetworkUnavailableError, ProviderError
from btc_backtest.signals.models import SignalQuery
from btc_backtest.signals.providers.binance import (
    BINANCE_FUNDING_URL,
    BINANCE_LONG_SHORT_URL,
    BINANCE_OPEN_INTEREST_URL,
    BINANCE_TAKER_FLOW_URL,
    BinanceDerivativesSignalProvider,
)
from pytest_httpx import HTTPXMock

UTC = timezone.utc


def timestamp_ms(hour: int = 0) -> int:
    value = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=hour)
    return int(value.timestamp() * 1_000)


def query(
    source_type: str,
    *,
    end: datetime = datetime(2024, 1, 2, tzinfo=UTC),
) -> SignalQuery:
    return SignalQuery(
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=end,
        symbol="BTC/USD",
        horizons=("1d",),
        source_types=(source_type,),
    )


def funding_payload(hour: int, rate: str = "0.0005") -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "fundingTime": timestamp_ms(hour),
        "fundingRate": rate,
    }


def test_funding_rate_maps_to_bounded_contrarian_direction(
    httpx_mock: HTTPXMock,
) -> None:
    raw = funding_payload(0)
    httpx_mock.add_response(json=[raw])

    observations = BinanceDerivativesSignalProvider(
        httpx.Client()
    ).fetch(query("funding"))

    assert len(observations) == 1
    item = observations[0]
    assert item.source_type == "funding"
    assert item.direction == Decimal("-1")
    assert item.confidence == Decimal("1")
    assert item.raw_value == Decimal("0.0005")
    assert item.observed_at == datetime(2024, 1, 1, tzinfo=UTC)
    assert item.effective_at == item.observed_at
    assert item.expires_at == datetime(2024, 1, 2, tzinfo=UTC)
    assert item.payload_sha256 == hashlib.sha256(
        json.dumps(
            raw,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert "contrarian" in item.quality_flags
    assert "scale_0_0005" in item.quality_flags
    request = httpx_mock.get_requests()[0]
    assert str(request.url).startswith(BINANCE_FUNDING_URL)
    assert "X-MBX-APIKEY" not in request.headers
    assert request.url.params["symbol"] == "BTCUSDT"
    assert request.url.params["startTime"] == str(timestamp_ms())
    assert request.url.params["endTime"] == str(timestamp_ms(23) + 3_599_999)


def test_open_interest_maps_change_against_previous_observation(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        json=[
            {
                "symbol": "BTCUSDT",
                "sumOpenInterest": "1000",
                "sumOpenInterestValue": "100",
                "timestamp": timestamp_ms(0),
            },
            {
                "symbol": "BTCUSDT",
                "sumOpenInterest": "1100",
                "sumOpenInterestValue": "110",
                "timestamp": timestamp_ms(1),
            },
        ],
    )

    observations = BinanceDerivativesSignalProvider(
        httpx.Client()
    ).fetch(query("open_interest"))

    assert str(httpx_mock.get_requests()[0].url).startswith(
        BINANCE_OPEN_INTEREST_URL
    )
    assert [item.direction for item in observations] == [
        Decimal("0"),
        Decimal("1"),
    ]
    assert [item.raw_value for item in observations] == [
        Decimal("100"),
        Decimal("110"),
    ]
    assert "insufficient_baseline" in observations[0].quality_flags
    assert "trend" in observations[1].quality_flags
    assert "scale_0_10_change" in observations[1].quality_flags


@pytest.mark.parametrize(
    ("source_type", "url", "payload", "expected_direction"),
    [
        (
            "long_short_ratio",
            BINANCE_LONG_SHORT_URL,
            {
                "symbol": "BTCUSDT",
                "longShortRatio": "3",
                "longAccount": "0.75",
                "shortAccount": "0.25",
                "timestamp": timestamp_ms(),
            },
            Decimal("-0.5"),
        ),
        (
            "taker_flow",
            BINANCE_TAKER_FLOW_URL,
            {
                "buySellRatio": "3",
                "buyVol": "75",
                "sellVol": "25",
                "timestamp": timestamp_ms(),
            },
            Decimal("0.5"),
        ),
    ],
)
def test_ratio_endpoints_have_explicit_bounded_mappings(
    httpx_mock: HTTPXMock,
    source_type: str,
    url: str,
    payload: dict[str, object],
    expected_direction: Decimal,
) -> None:
    httpx_mock.add_response(json=[payload])

    item = BinanceDerivativesSignalProvider(
        httpx.Client()
    ).fetch(query(source_type))[0]

    assert str(httpx_mock.get_requests()[0].url).startswith(url)
    assert item.direction == expected_direction
    assert item.raw_value == Decimal("3")
    assert item.confidence == abs(expected_direction)
    assert (
        ("contrarian" in item.quality_flags)
        if source_type == "long_short_ratio"
        else ("trend" in item.quality_flags)
    )


def test_provider_paginates_from_last_observed_timestamp(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        json=[funding_payload(0), funding_payload(8)]
    )
    httpx_mock.add_response(json=[funding_payload(16)])

    observations = BinanceDerivativesSignalProvider(
        httpx.Client(),
        page_size=2,
    ).fetch(query("funding"))

    assert len(observations) == 3
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    assert requests[0].url.params["startTime"] == str(timestamp_ms(0))
    assert requests[1].url.params["startTime"] == str(timestamp_ms(8) + 1)
    assert all(request.url.params["limit"] == "2" for request in requests)


def test_provider_retries_rate_limit_then_succeeds(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(status_code=429)
    httpx_mock.add_response(
        json=[funding_payload(0)],
    )

    result = BinanceDerivativesSignalProvider(
        httpx.Client(),
        retry_backoff=0.0,
    ).fetch(query("funding"))

    assert len(result) == 1
    assert len(httpx_mock.get_requests()) == 2


def test_provider_bounds_transport_retries() -> None:
    attempts = 0

    def fail_transport(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(fail_transport)) as client,
        pytest.raises(NetworkUnavailableError, match="unreachable"),
    ):
        BinanceDerivativesSignalProvider(
            client,
            max_retries=2,
            retry_backoff=0.0,
        ).fetch(query("funding"))

    assert attempts == 3


def test_provider_rejects_timestamp_outside_query(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        json=[funding_payload(24)],
    )

    with pytest.raises(ProviderError, match="outside query"):
        BinanceDerivativesSignalProvider(httpx.Client()).fetch(
            query("funding")
        )


def test_provider_rejects_malformed_payload(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        json=[{"symbol": "BTCUSDT", "fundingTime": timestamp_ms()}],
    )

    with pytest.raises(ProviderError, match="malformed funding"):
        BinanceDerivativesSignalProvider(httpx.Client()).fetch(
            query("funding")
        )


def test_provider_rejects_unsupported_identity() -> None:
    unsupported_symbol = query("funding").model_copy(
        update={"symbol": "ETH/USD"}
    )
    unsupported_source = query("funding").model_copy(
        update={"source_types": ("news",)}
    )

    with pytest.raises(ProviderError, match="symbol"):
        BinanceDerivativesSignalProvider(httpx.Client()).fetch(
            unsupported_symbol
        )
    with pytest.raises(ProviderError, match="source"):
        BinanceDerivativesSignalProvider(httpx.Client()).fetch(
            unsupported_source
        )
