from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from btc_backtest.errors import ProviderError
from btc_backtest.signals.models import SignalQuery
from btc_backtest.signals.providers.alternative import (
    ALTERNATIVE_FEAR_GREED_URL,
    FearGreedSignalProvider,
)
from pytest_httpx import HTTPXMock

UTC = timezone.utc


def query() -> SignalQuery:
    return SignalQuery(
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 3, tzinfo=UTC),
        symbol="BTC/USD",
        horizons=("1d",),
        source_types=("sentiment",),
    )


def row(
    day: int,
    value: str,
    classification: str = "Fear",
) -> dict[str, str]:
    timestamp = int(datetime(2024, 1, day, tzinfo=UTC).timestamp())
    return {
        "value": value,
        "value_classification": classification,
        "timestamp": str(timestamp),
        "time_until_update": "0",
    }


def test_fear_greed_maps_extremes_and_timestamp(
    httpx_mock: HTTPXMock,
) -> None:
    raw = row(1, "10", "Extreme Fear")
    httpx_mock.add_response(json={"data": [raw]})

    item = FearGreedSignalProvider(httpx.Client()).fetch(query())[0]

    assert item.provider == "alternative_fear_greed"
    assert item.source_type == "sentiment"
    assert item.direction == Decimal("0.8")
    assert item.confidence == Decimal("0.8")
    assert item.raw_value == Decimal("10")
    assert item.observed_at == datetime(2024, 1, 1, tzinfo=UTC)
    assert item.effective_at == item.observed_at
    assert item.payload_sha256 == hashlib.sha256(
        json.dumps(
            raw,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert "contrarian" in item.quality_flags
    request = httpx_mock.get_requests()[0]
    assert str(request.url).startswith(ALTERNATIVE_FEAR_GREED_URL)
    assert request.url.params["limit"] == "0"
    assert request.url.params["format"] == "json"
    assert "Authorization" not in request.headers


def test_fear_greed_filters_full_history_and_sorts_ascending(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        json={
            "data": [
                row(3, "50", "Neutral"),
                row(2, "80", "Extreme Greed"),
                row(1, "20", "Extreme Fear"),
            ]
        }
    )

    observations = FearGreedSignalProvider(httpx.Client()).fetch(query())

    assert [item.observed_at.day for item in observations] == [1, 2]
    assert [item.direction for item in observations] == [
        Decimal("0.6"),
        Decimal("-0.6"),
    ]


@pytest.mark.parametrize("value", ["-1", "101", "NaN", "not-a-number"])
def test_fear_greed_rejects_invalid_values(
    httpx_mock: HTTPXMock,
    value: str,
) -> None:
    httpx_mock.add_response(json={"data": [row(1, value)]})

    with pytest.raises(ProviderError, match="value"):
        FearGreedSignalProvider(httpx.Client()).fetch(query())


def test_fear_greed_rejects_malformed_response(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(json={"metadata": {"error": None}})

    with pytest.raises(ProviderError, match="malformed"):
        FearGreedSignalProvider(httpx.Client()).fetch(query())


def test_fear_greed_retries_rate_limit(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(status_code=429)
    httpx_mock.add_response(json={"data": [row(1, "25")]})

    result = FearGreedSignalProvider(
        httpx.Client(),
        retry_backoff=0.0,
    ).fetch(query())

    assert len(result) == 1
    assert len(httpx_mock.get_requests()) == 2


def test_fear_greed_rejects_unsupported_query() -> None:
    wrong_symbol = query().model_copy(update={"symbol": "ETH/USD"})
    wrong_source = query().model_copy(
        update={"source_types": ("onchain",)}
    )

    with pytest.raises(ProviderError, match="symbol"):
        FearGreedSignalProvider(httpx.Client()).fetch(wrong_symbol)
    with pytest.raises(ProviderError, match="source"):
        FearGreedSignalProvider(httpx.Client()).fetch(wrong_source)
