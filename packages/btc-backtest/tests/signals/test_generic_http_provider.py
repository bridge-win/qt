from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from btc_backtest.errors import ProviderError
from btc_backtest.signals.models import SignalQuery
from btc_backtest.signals.providers.generic_http import (
    EnvironmentHeader,
    GenericJSONFieldMap,
    GenericJSONSignalProvider,
    JSONProviderConfig,
)
from pydantic import ValidationError
from pytest_httpx import HTTPXMock

UTC = timezone.utc


def query() -> SignalQuery:
    return SignalQuery(
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 3, tzinfo=UTC),
        symbol="BTC/USD",
        horizons=("1d",),
        source_types=("research",),
    )


def event(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "event": {"id": "remote-1", "type": "research"},
        "asset": "BTC/USD",
        "forecast": {
            "horizon": "1d",
            "direction": "0.6",
            "confidence": "0.75",
            "raw": "12",
        },
        "availability": {
            "effective": "2024-01-01T00:00:00Z",
            "observed": "2024-01-02T00:00:00Z",
            "expires": "2024-01-04T00:00:00Z",
        },
        "source": {"url": "https://feed.example/events/remote-1"},
    }
    values.update(updates)
    return values


def config() -> JSONProviderConfig:
    return JSONProviderConfig(
        id="research_feed",
        description="Test research signal feed",
        url="https://api.example/signals",
        allowed_hosts=("api.example",),
        source_types=("research",),
        items_path="payload.items",
        next_cursor_path="paging.next",
        cursor_param="cursor",
        headers=(
            EnvironmentHeader(
                name="Authorization",
                environment="TEST_SIGNAL_TOKEN",
                prefix="Bearer ",
            ),
        ),
        fields=GenericJSONFieldMap(
            source_event_id="event.id",
            source_type="event.type",
            symbol="asset",
            horizon="forecast.horizon",
            direction="forecast.direction",
            confidence="forecast.confidence",
            raw_value="forecast.raw",
            effective_at="availability.effective",
            observed_at="availability.observed",
            expires_at="availability.expires",
            provenance="source.url",
        ),
    )


def test_generic_json_uses_environment_secret_without_serializing_it(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_SIGNAL_TOKEN", "secret")
    httpx_mock.add_response(
        json={"payload": {"items": [event()]}, "paging": {"next": None}}
    )

    provider = GenericJSONSignalProvider(config(), httpx.Client())
    item = provider.fetch(query())[0]

    assert item.provider == "research_feed"
    assert item.direction == Decimal("0.6")
    assert "secret" not in item.model_dump_json()
    assert "secret" not in provider.config.model_dump_json()
    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer secret"


def test_generic_json_follows_bounded_cursor_pages(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_SIGNAL_TOKEN", "secret")
    httpx_mock.add_response(
        json={
            "payload": {"items": [event()]},
            "paging": {"next": "page-two"},
        }
    )
    second = event()
    second["event"] = {"id": "remote-2", "type": "research"}
    second["availability"] = {
        "effective": "2024-01-02T00:00:00Z",
        "observed": "2024-01-02T12:00:00Z",
        "expires": "2024-01-05T00:00:00Z",
    }
    httpx_mock.add_response(
        json={
            "payload": {"items": [second]},
            "paging": {"next": None},
        }
    )

    observations = GenericJSONSignalProvider(
        config(),
        httpx.Client(),
    ).fetch(query())

    assert len(observations) == 2
    requests = httpx_mock.get_requests()
    assert "cursor" not in requests[0].url.params
    assert requests[1].url.params["cursor"] == "page-two"


def test_generic_json_rejects_missing_availability(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_SIGNAL_TOKEN", "secret")
    missing = event()
    missing["availability"] = {
        "effective": "2024-01-01T00:00:00Z",
        "expires": "2024-01-04T00:00:00Z",
    }
    httpx_mock.add_response(
        json={
            "payload": {"items": [missing]},
            "paging": {"next": None},
        }
    )

    with pytest.raises(ProviderError, match="observed_at"):
        GenericJSONSignalProvider(config(), httpx.Client()).fetch(query())


def test_generic_json_requires_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_SIGNAL_TOKEN", raising=False)

    with pytest.raises(ProviderError, match="TEST_SIGNAL_TOKEN"):
        GenericJSONSignalProvider(config(), httpx.Client()).fetch(query())


@pytest.mark.parametrize(
    ("url", "hosts"),
    [
        ("http://api.example/signals", ("api.example",)),
        ("https://evil.example/signals", ("api.example",)),
        ("https://user:password@api.example/signals", ("api.example",)),
    ],
)
def test_generic_json_config_rejects_unsafe_url(
    url: str,
    hosts: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        config().model_copy(
            update={"url": url, "allowed_hosts": hosts}
        ).model_validate(
            {
                **config().model_dump(),
                "url": url,
                "allowed_hosts": hosts,
            }
        )


def test_generic_json_rejects_cursor_stall(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_SIGNAL_TOKEN", "secret")
    page = {
        "payload": {"items": []},
        "paging": {"next": "same"},
    }
    httpx_mock.add_response(json=page)
    httpx_mock.add_response(json=page)

    with pytest.raises(ProviderError, match="cursor"):
        GenericJSONSignalProvider(config(), httpx.Client()).fetch(query())
