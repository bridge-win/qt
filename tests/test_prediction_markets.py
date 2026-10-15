from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from qt.prediction_markets.polymarket import (
    CLOB_URL,
    GAMMA_URL,
    MAX_BYTES,
    PublicDataError,
    fetch_snapshot,
    parse_book,
    parse_market,
    validate_slug,
)
from qt.workbench.api import WorkbenchSettings, create_workbench_app

NOW = 1_800_000_000.0
CONDITION = "0x" + "a" * 64
SLUG = "btc-above-threshold"


def market(version="v1", **changes):
    data = {
        "slug": SLUG, "conditionId": CONDITION, "question": "Will BTC exceed the threshold?",
        "version": version, "outcomes": '["Yes", "No"]', "clobTokenIds": '["111", "222"]',
        "positionIds": ["333", "444"], "active": True, "closed": False,
        "acceptingOrders": True, "enableOrderBook": True, "restricted": False,
    }
    data.update(changes)
    return data


def book(token="111", **changes):
    data = {
        "asset_id": token, "market": CONDITION, "timestamp": str(int(NOW * 1000)),
        # Actual API ordering: bids ascending, asks descending.
        "bids": [{"price": "0.1", "size": "5"}, {"price": "0.4", "size": "10"}],
        "asks": [{"price": "0.9", "size": "2"}, {"price": "0.6", "size": "20"}],
    }
    data.update(changes)
    return data


def test_explicit_schema_version_selects_correct_tokens_not_field_presence():
    assert [quote.token_id for quote in parse_market(market(), SLUG, NOW).outcomes] == ["111", "222"]
    assert [quote.token_id for quote in parse_market(market("v2"), SLUG, NOW).outcomes] == ["333", "444"]
    legacy = market()
    legacy.pop("version")
    assert parse_market(legacy, SLUG, NOW).version == "v1"


@pytest.mark.parametrize("slug", ["https://example.com", "../secret", "ABC", "has_space ", "two--dashes", "a" * 201, ""])
def test_slugs_are_identifiers_not_arbitrary_urls(slug):
    with pytest.raises(PublicDataError):
        validate_slug(slug)


@pytest.mark.parametrize("changes", [
    {"version": "v3"}, {"version": []}, {"version": {}}, {"version": None},
    {"slug": "different"}, {"conditionId": "0x123"}, {"outcomes": "not-json"},
    {"outcomes": '["Yes"]'}, {"outcomes": '["Yes", "Yes"]'},
    {"clobTokenIds": '["111", "111"]'}, {"clobTokenIds": '["111", "invalid"]'},
    {"positionIds": '["333", "444"]', "version": "v2"},
    {"active": "true"}, {"acceptingOrders": None}, {"enableOrderBook": 1},
])
def test_malformed_identity_and_schema_fail_closed(changes):
    with pytest.raises(PublicDataError):
        parse_market(market(**changes), SLUG, NOW)


@pytest.mark.parametrize("changes,reason", [
    ({"active": False}, "market_inactive"), ({"closed": True}, "market_closed"),
    ({"acceptingOrders": False}, "market_not_accepting_orders"),
    ({"archived": True}, "market_archived"), ({"enableOrderBook": False}, "orderbook_disabled"),
])
def test_closed_disabled_inactive_markets_never_ready(changes, reason):
    snapshot = parse_market(market(**changes), SLUG, NOW)
    assert not snapshot.active and not snapshot.ready and reason in snapshot.reasons


def test_restricted_is_metadata_not_public_data_denial_or_trading_permission():
    snapshot = parse_market(market(restricted=True), SLUG, NOW)
    assert snapshot.active and snapshot.restricted
    assert "market_restrictions_apply" in snapshot.reasons
    assert snapshot.live_execution_enabled is False


def test_deeply_encoded_market_arrays_are_sanitized():
    for field in ("outcomes", "clobTokenIds"):
        with pytest.raises(PublicDataError):
            parse_market(market(**{field: "[" * 20000 + "]" * 20000}), SLUG, NOW)


def test_book_uses_max_bid_min_ask_without_assumed_sort_order_or_complement():
    snapshot = parse_market(market(), SLUG, NOW)
    result = parse_book(book(), snapshot, snapshot.outcomes[0], NOW)
    assert result.bid == 0.4 and result.ask == 0.6 and result.midpoint == 0.5 and result.ready
    empty = parse_book(book(bids=[]), snapshot, snapshot.outcomes[0], NOW)
    assert empty.bid is None and empty.midpoint is None and not empty.ready
    assert snapshot.outcomes[1].bid is None  # No invented complementary prices.


@pytest.mark.parametrize("changes", [
    {"asset_id": "222"}, {"market": "0x" + "b" * 64}, {"timestamp": True},
    {"bids": [{"price": "NaN", "size": "1"}]}, {"asks": [{"price": "1.1", "size": "1"}]},
    {"bids": [{"price": True, "size": "1"}]}, {"bids": [{"price": "0.4", "size": "-1"}]},
    {"bids": [{"price": "0.7", "size": "1"}]}, {"asks": None},
])
def test_malformed_wrong_identity_or_crossed_books_rejected(changes):
    snapshot = parse_market(market(), SLUG, NOW)
    with pytest.raises(PublicDataError):
        parse_book(book(**changes), snapshot, snapshot.outcomes[0], NOW)


@pytest.mark.parametrize("offset,reason", [(-16, "orderbook_stale"), (3, "orderbook_future")])
def test_stale_or_future_book_not_ready(offset, reason):
    snapshot = parse_market(market(), SLUG, NOW)
    result = parse_book(book(timestamp=str(int((NOW + offset) * 1000))), snapshot, snapshot.outcomes[0], NOW)
    assert not result.ready and reason in result.reasons


@pytest.mark.asyncio
async def test_public_fetch_uses_only_allowlisted_read_endpoints_and_exact_ids(monkeypatch):
    from qt.prediction_markets import polymarket

    requests = []
    monkeypatch.setattr(polymarket.time, "time", lambda: NOW)

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and "authorization" not in request.headers
        if request.url.host == "gamma-api.polymarket.com":
            assert str(request.url) == f"{GAMMA_URL}/markets/slug/{SLUG}"
            return httpx.Response(200, json=market("v2", restricted=True))
        assert str(request.url).startswith(f"{CLOB_URL}/book?token_id=")
        return httpx.Response(200, json=book(request.url.params["token_id"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        snapshot = await fetch_snapshot(SLUG, client=client)
    assert len(requests) == 3 and snapshot.ready and snapshot.environment == "production-public"
    assert snapshot.restricted and snapshot.live_execution_enabled is False


@pytest.mark.asyncio
async def test_inactive_market_does_not_fetch_orderbooks():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=market(closed=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        snapshot = await fetch_snapshot(SLUG, client=client)
    assert len(requests) == 1 and not snapshot.ready


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [(b"[]", 403), (b'{"error":"denied"}', 302), (b"x" * (MAX_BYTES + 1), 200), ((b"[" * 2000) + (b"]" * 2000), 200)], ids=["forbidden", "redirect", "oversize", "deep"])
async def test_errors_redirects_large_or_deep_json_are_sanitized(body, status):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(status, content=body, headers={"location": "https://elsewhere.invalid"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        with pytest.raises(PublicDataError):
            await fetch_snapshot(SLUG, client=client)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_one_outcome_failure_never_invents_opposite_probability(monkeypatch):
    from qt.prediction_markets import polymarket

    monkeypatch.setattr(polymarket.time, "time", lambda: NOW)

    def handle(request):
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=market())
        if request.url.params["token_id"] == "222":
            return httpx.Response(404)
        return httpx.Response(200, json=book())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        snapshot = await fetch_snapshot(SLUG, client=client)
    assert not snapshot.ready and snapshot.outcomes[0].ready
    assert snapshot.outcomes[1].midpoint is None and not snapshot.outcomes[1].ready


@pytest.mark.asyncio
async def test_first_outcome_expiry_is_rechecked_after_second_fetch(monkeypatch):
    from qt.prediction_markets import polymarket

    clock = [NOW]
    monkeypatch.setattr(polymarket.time, "time", lambda: clock[0])

    def handle(request):
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=market())
        token = request.url.params["token_id"]
        if token == "222":
            clock[0] += 16
        return httpx.Response(200, json=book(token, timestamp=str(int(clock[0] * 1000))))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        snapshot = await fetch_snapshot(SLUG, client=client)
    assert not snapshot.ready and "orderbook_stale" in snapshot.outcomes[0].reasons


def test_workbench_prediction_route_obeys_auth_and_read_only_boundary(tmp_path, monkeypatch):
    from qt.prediction_markets import api

    async def fetch(slug):
        return parse_market(market(), slug, NOW)

    monkeypatch.setattr(api, "fetch_snapshot", fetch)
    app = create_workbench_app(WorkbenchSettings(parquet_root=tmp_path / "parquet", state_root=tmp_path / "state", origin_client_id="fixture-id", origin_client_secret="fixture-secret"))
    with TestClient(app) as client:
        path = f"/api/v3/prediction-markets/polymarket/{SLUG}"
        assert client.get(path).status_code == 401
        headers = {"cf-access-client-id": "fixture-id", "cf-access-client-secret": "fixture-secret", "x-qt-access-subject": "fixture-user"}
        response = client.get(path, headers=headers)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["live_execution_enabled"] is False
        assert client.post(path, headers=headers).status_code == 405
        assert client.get("/api/v3/prediction-markets/polymarket/ABC", headers=headers).status_code == 422
