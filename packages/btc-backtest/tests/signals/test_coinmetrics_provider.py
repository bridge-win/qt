from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from btc_backtest.errors import ProviderError
from btc_backtest.signals.models import SignalQuery
from btc_backtest.signals.providers.coinmetrics import (
    COIN_METRICS_ASSET_METRICS_URL,
    CoinMetricsSignalProvider,
    MetricRule,
)
from pytest_httpx import HTTPXMock

UTC = timezone.utc


def query() -> SignalQuery:
    return SignalQuery(
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 3, tzinfo=UTC),
        symbol="BTC/USD",
        horizons=("1d",),
        source_types=("onchain",),
    )


def price_rule() -> MetricRule:
    return MetricRule(
        source_field="PriceUSD",
        transform="centered",
        direction="trend",
        horizon="1d",
        expiry=timedelta(days=2),
        center=Decimal("40000"),
        scale=Decimal("10000"),
        minimum=Decimal("0"),
    )


def mvrv_rule() -> MetricRule:
    return MetricRule(
        source_field="CapMVRVCur",
        transform="centered",
        direction="contrarian",
        horizon="1d",
        expiry=timedelta(days=2),
        center=Decimal("1"),
        scale=Decimal("1"),
        minimum=Decimal("0"),
    )


def provider(
    client: httpx.Client | None = None,
    *,
    page_size: int = 100,
) -> CoinMetricsSignalProvider:
    return CoinMetricsSignalProvider(
        {"price": price_rule()},
        client or httpx.Client(),
        page_size=page_size,
        retry_backoff=0.0,
    )


def test_coinmetrics_uses_response_time_when_status_time_is_missing(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        headers={"Date": "Tue, 02 Jan 2024 00:00:00 GMT"},
        json={
            "data": [
                {
                    "asset": "btc",
                    "time": "2024-01-01T00:00:00Z",
                    "PriceUSD": "42000",
                }
            ]
        },
    )

    item = provider().fetch(query())[0]

    assert item.provider == "coinmetrics"
    assert item.source_type == "onchain"
    assert item.direction == Decimal("0.2")
    assert item.raw_value == Decimal("42000")
    assert item.effective_at == datetime(2024, 1, 1, tzinfo=UTC)
    assert item.observed_at == datetime(2024, 1, 2, tzinfo=UTC)
    assert item.expires_at == datetime(2024, 1, 4, tzinfo=UTC)
    assert "delayed_observation" in item.quality_flags


def test_coinmetrics_prefers_metric_status_time_and_maps_contrarian_rule(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        headers={"Date": "Wed, 03 Jan 2024 00:00:00 GMT"},
        json={
            "data": [
                {
                    "asset": "btc",
                    "time": "2024-01-01T00:00:00.000000000Z",
                    "CapMVRVCur": "0.5",
                    "CapMVRVCur-status": "reviewed",
                    "CapMVRVCur-status-time": (
                        "2024-01-02T09:59:13.847251000Z"
                    ),
                }
            ]
        },
    )
    configured = CoinMetricsSignalProvider(
        {"mvrv": mvrv_rule()},
        httpx.Client(),
    )

    item = configured.fetch(query())[0]

    assert item.direction == Decimal("0.5")
    assert item.observed_at == datetime(
        2024,
        1,
        2,
        9,
        59,
        13,
        847251,
        tzinfo=UTC,
    )
    assert "status_reviewed" in item.quality_flags
    assert "delayed_observation" not in item.quality_flags
    assert "contrarian" in item.quality_flags


def test_coinmetrics_requests_only_allowlisted_fields(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        headers={"Date": "Tue, 02 Jan 2024 00:00:00 GMT"},
        json={"data": []},
    )
    configured = CoinMetricsSignalProvider(
        {
            "mvrv": mvrv_rule(),
            "price": price_rule(),
        },
        httpx.Client(),
    )

    assert configured.fetch(query()) == ()

    request = httpx_mock.get_requests()[0]
    assert str(request.url).startswith(COIN_METRICS_ASSET_METRICS_URL)
    assert request.url.params["assets"] == "btc"
    assert request.url.params["metrics"] == "CapMVRVCur,PriceUSD"
    assert request.url.params["frequency"] == "1d"
    assert request.url.params["paging_from"] == "start"
    assert request.url.params["sort"] == "time"
    assert "api_key" not in request.url.params


def test_coinmetrics_follows_bounded_page_tokens(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        headers={"Date": "Tue, 02 Jan 2024 00:00:00 GMT"},
        json={
            "data": [
                {
                    "asset": "btc",
                    "time": "2024-01-01T00:00:00Z",
                    "PriceUSD": "41000",
                }
            ],
            "next_page_token": "page-two",
            "next_page_url": (
                f"{COIN_METRICS_ASSET_METRICS_URL}"
                "?next_page_token=page-two"
            ),
        },
    )
    httpx_mock.add_response(
        headers={"Date": "Wed, 03 Jan 2024 00:00:00 GMT"},
        json={
            "data": [
                {
                    "asset": "btc",
                    "time": "2024-01-02T00:00:00Z",
                    "PriceUSD": "42000",
                }
            ]
        },
    )

    observations = provider(page_size=1).fetch(query())

    assert len(observations) == 2
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    assert "next_page_token" not in requests[0].url.params
    assert requests[1].url.params["next_page_token"] == "page-two"


def test_coinmetrics_skips_explicit_null_metric(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        headers={"Date": "Tue, 02 Jan 2024 00:00:00 GMT"},
        json={
            "data": [
                {
                    "asset": "btc",
                    "time": "2024-01-01T00:00:00Z",
                    "PriceUSD": None,
                }
            ]
        },
    )

    assert provider().fetch(query()) == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"asset": "eth", "time": "2024-01-01T00:00:00Z", "PriceUSD": "1"}]},
        {"data": [{"asset": "btc", "time": "2024-01-03T00:00:00Z", "PriceUSD": "1"}]},
        {"data": [{"asset": "btc", "time": "invalid", "PriceUSD": "1"}]},
        {"data": [{"asset": "btc", "time": "2024-01-01T00:00:00Z"}]},
    ],
)
def test_coinmetrics_rejects_malformed_or_out_of_scope_rows(
    httpx_mock: HTTPXMock,
    payload: dict[str, object],
) -> None:
    httpx_mock.add_response(
        headers={"Date": "Tue, 02 Jan 2024 00:00:00 GMT"},
        json=payload,
    )

    with pytest.raises(ProviderError):
        provider().fetch(query())


def test_metric_rule_rejects_unsafe_or_undefined_configuration() -> None:
    with pytest.raises(ValueError, match="scale"):
        MetricRule(
            source_field="PriceUSD",
            transform="centered",
            direction="trend",
            horizon="1d",
            expiry=timedelta(days=1),
            center=Decimal("0"),
            scale=Decimal("0"),
        )
    with pytest.raises(ValueError, match="source field"):
        MetricRule(
            source_field="bad,field",
            transform="centered",
            direction="trend",
            horizon="1d",
            expiry=timedelta(days=1),
            center=Decimal("0"),
            scale=Decimal("1"),
        )


def test_coinmetrics_rejects_unsupported_query() -> None:
    wrong_horizon = query().model_copy(update={"horizons": ("8h",)})
    wrong_source = query().model_copy(
        update={"source_types": ("sentiment",)}
    )

    with pytest.raises(ProviderError, match="horizon"):
        provider().fetch(wrong_horizon)
    with pytest.raises(ProviderError, match="source"):
        provider().fetch(wrong_source)
