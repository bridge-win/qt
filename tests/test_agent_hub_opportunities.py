"""Public screening is point-in-time evidence, never an order authorization."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from qt.agent_hub.opportunities import (
    OpportunityScanner,
    OpportunityStore,
    _hash,
    book_liquidity,
    btc_features,
    prediction_costs,
)

CONDITION = "0x" + "a" * 64


def candles() -> list[list]:
    today = int(time.time() // 86400) * 86400000
    return [
        [
            today - (100 - i) * 86400000,
            40000 + i,
            40010 + i,
            39990 + i,
            40001 + i,
            10,
            today - (99 - i) * 86400000 - 1,
        ]
        for i in range(100)
    ]


def market() -> dict:
    return {
        "slug": "btc-next-month",
        "question": "Will BTC exceed a specified price?",
        "conditionId": CONDITION,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["123", "456"]',
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "restricted": False,
        "endDate": datetime.fromtimestamp(time.time() + 86400, timezone.utc).isoformat(),
        "resolutionSource": "https://example.com/official-resolution",
        "description": "Settle by official closing price.",
    }


def book(token: str = "123") -> dict:
    return {
        "asset_id": token,
        "market": CONDITION,
        "timestamp": str(int(time.time() * 1000)),
        "bids": [{"price": "0.49", "size": "200"}],
        "asks": [{"price": "0.50", "size": "200"}],
    }


def transport(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert "authorization" not in request.headers
    if request.url.path == "/api/v3/klines":
        payload = candles()
    elif request.url.path == "/api/v3/depth":
        payload = {"bids": [["40000", "2"]], "asks": [["40001", "2"]]}
    elif request.url.path == "/markets":
        payload = [market()]
    elif request.url.path.startswith("/clob-markets/"):
        payload = {"t": [{"t": "123"}, {"t": "456"}], "fd": {"r": 0.07, "e": 1}}
    elif request.url.path == "/book":
        payload = book(request.url.params["token_id"])
    else:
        raise AssertionError(request.url)
    return httpx.Response(200, json=payload)


async def scan_with(handler=transport) -> dict:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await OpportunityScanner(client).scan_async("personal", limit=1)


def test_full_screen_records_sources_depth_costs_without_fabricated_alpha() -> None:
    result = asyncio.run(scan_with())
    assert result["status"] == "complete"
    assert len(result["candidates"]) == 3
    assert len(result["sources"]) == 6
    assert result["sha256"] == _hash({k: v for k, v in result.items() if k != "sha256"})
    for candidate in result["candidates"]:
        assert candidate["status"] == "research_required"
        assert candidate["estimated_probability"] is None
        assert candidate["net_edge"] is None
        assert candidate["execution_authorized"] is False
        assert candidate["observed_at"] >= candidate["available_at"]
    prediction = result["candidates"][-1]
    assert prediction["costs"]["fee_per_share"] == pytest.approx(0.0175)
    for source in result["sources"]:
        assert source["payload_sha256"] == _hash(source["payload"])


def test_btc_open_bars_excluded_and_gaps_rejected() -> None:
    rows = candles()
    now = time.time()
    rows.append([int(now * 1000), 1, 2, 1, 2, 1, int(now * 1000) + 86399999])
    assert btc_features(rows, as_of=now)["closed_bars"] == 100
    rows.pop(5)
    with pytest.raises(ValueError, match="gapped"):
        btc_features(rows, as_of=now)


@pytest.mark.parametrize("change", ["nan", "ohlc", "stale", "wrong_interval"])
def test_bad_history_rejected(change: str) -> None:
    rows = candles()
    if change == "nan":
        rows[-1][4] = float("nan")
    elif change == "ohlc":
        rows[-1][2] = 1
    elif change == "wrong_interval":
        rows[-1][6] -= 1
    else:
        rows = rows[:-10]
    with pytest.raises(ValueError):
        btc_features(rows, as_of=time.time())


def test_depth_walk_has_real_notional_impact_and_insufficient_depth() -> None:
    data = {"bids": [["99", "5"]], "asks": [["100", "0.1"], ["110", "1"]]}
    result = book_liquidity(data, prediction=False, quote_budget=21)
    assert result["buy_vwap"] == 105
    assert result["impact_bps"] == pytest.approx(500)
    assert book_liquidity(data, prediction=False, quote_budget=200)["buy_vwap"] is None
    with pytest.raises(ValueError, match="crossed"):
        book_liquidity({"bids": [["101", "1"]], "asks": [["100", "1"]]}, prediction=False)


@pytest.mark.parametrize(
    "payload",
    [
        {"base_fee": 1000},
        {"t": [{"t": "123"}], "fd": None},
        {"t": [{"t": "123"}], "fd": {"r": 0.07, "e": 2}},
        {"t": [{"t": "999"}], "fd": {"r": 0.07, "e": 1}},
    ],
)
def test_unknown_fee_data_never_becomes_free_trading(payload: dict) -> None:
    assert prediction_costs(payload, "123", 0.5)["known"] is False


def test_429_and_missing_books_are_diagnostic_partial_results() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/book":
            return httpx.Response(404, json={"error": "secret-looking-upstream-body"})
        if request.url.path == "/api/v3/klines":
            return httpx.Response(429)
        return transport(request)

    result = asyncio.run(scan_with(failing))
    assert result["status"] == "unavailable"
    assert {item["reason"] for item in result["errors"]} == {"public_http_429", "public_http_404"}
    assert "secret-looking" not in json.dumps(result)


def test_invalid_fees_and_expired_maturity_block_candidates() -> None:
    def invalid(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets":
            value = market()
            value["endDate"] = "2020-01-01T00:00:00Z"
            return httpx.Response(200, json=[value])
        if request.url.path.startswith("/clob-markets/"):
            return httpx.Response(200, json={"base_fee": 1000})
        return transport(request)

    result = asyncio.run(scan_with(invalid))
    predictions = [item for item in result["candidates"] if item["venue"] == "polymarket"]
    assert all(item["status"] == "blocked" for item in predictions)
    assert all("fee_model_unverified" in item["reasons"] for item in predictions)
    assert all(
        "maturity_unknown_or_outside_research_window" in item["reasons"] for item in predictions
    )


def test_deadline_bounds_whole_scan_and_retains_failure() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=[])

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await OpportunityScanner(client).scan_async(
                "personal", limit=1, deadline_seconds=1
            )

    started = time.monotonic()
    result = asyncio.run(run())
    assert time.monotonic() - started < 2
    assert result["errors"] == [{"source": "scan", "reason": "scan_deadline_exceeded"}]


def test_store_deduplicates_identity_retains_observations_and_tenant_boundary(
    tmp_path: Path,
) -> None:
    store = OpportunityStore(tmp_path / "opportunities.sqlite3")
    assert store.summary("personal")["latest_scan"] is None
    first = asyncio.run(scan_with())
    second = asyncio.run(scan_with())
    returned = store.record("personal", first)
    assert "sources" not in returned and returned["id"] == first["id"]
    store.record("personal", first)
    store.record("personal", second)
    assert store.summary("personal")["scan_count"] == 2
    assert len(store.list("personal")) == 3
    assert all(item["source_scan_id"] == second["id"] for item in store.list("personal"))
    assert store.latest_scan("personal")["id"] == second["id"]
    assert store.scan_detail("personal", first["id"])["sources"] == first["sources"]
    candidate = first["candidates"][0]
    assert store.get("personal", candidate["id"])["source_scan_id"] == second["id"]
    assert "source_scan_id" not in store.scan_detail("personal", first["id"])["candidates"][0]
    assert store.get("other", candidate["id"]) is None
    assert store.scan_detail("other", first["id"]) is None
    assert store.list("other") == []
    updated = store.review(
        "personal", candidate["id"], status="watching", note="Compare a registered baseline."
    )
    assert updated["review"]["status"] == "watching"
    with pytest.raises(ValueError, match="invalid_research_review"):
        store.review("personal", candidate["id"], status="approved")
    with pytest.raises(ValueError, match="tenant"):
        store.record("other", first)


def test_store_tamper_is_rejected_and_expired_observations_are_visible(tmp_path: Path) -> None:
    result = asyncio.run(scan_with())
    store = OpportunityStore(tmp_path / "opportunities.sqlite3")
    tampered = copy.deepcopy(result)
    tampered["candidates"][0]["net_edge"] = 0.3
    with pytest.raises(ValueError, match="hash"):
        store.record("personal", tampered)
    store.record("personal", result)
    candidate = result["candidates"][0]
    candidate["expires_at"] = 1
    assert store._current("personal", candidate)["stale"] is True


def test_scan_ids_cannot_be_reused_for_different_observations(tmp_path: Path) -> None:
    first = asyncio.run(scan_with())
    second = asyncio.run(scan_with())
    second["id"] = first["id"]
    second["sha256"] = _hash({k: v for k, v in second.items() if k != "sha256"})
    store = OpportunityStore(tmp_path / "opportunities.sqlite3")
    store.record("personal", first)
    with pytest.raises(ValueError, match="identity_collision"):
        store.record("personal", second)
    assert store.summary("personal")["scan_count"] == 1


def test_source_archive_integrity_is_checked_independently(tmp_path: Path) -> None:
    scan = asyncio.run(scan_with())
    scan["sources"][0]["payload"] = {"changed": True}
    scan["sha256"] = _hash({k: v for k, v in scan.items() if k != "sha256"})
    with pytest.raises(ValueError, match="source_payload_hash_mismatch"):
        OpportunityStore(tmp_path / "opportunities.sqlite3").record("personal", scan)
