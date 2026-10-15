"""Offline selected-venue public signal contracts, separate from demo execution."""

from __future__ import annotations

import httpx
import pytest

from qt.btc_agent import market


def _get(monkeypatch: pytest.MonkeyPatch, data: object) -> list[tuple[str, dict[str, object]]]:
    calls: list[tuple[str, dict[str, object]]] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        calls.append((url, kwargs))
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    return calls


def test_okx_confirmed_daily_utc_base_volume_and_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _get(monkeypatch, {"code": "0", "data": [
        ["172800000", "12", "14", "11", "13", "3", "3", "39", "0"],
        ["86400000", "11", "13", "10", "12", "2", "2", "24", "1"],
        ["0", "10", "12", "9", "11", "1", "1", "11", "1"],
    ]})
    candles = market.okx_candles()
    assert [c.open_time for c in candles] == [0, 86400000]
    assert [c.volume for c in candles] == [1, 2]
    assert calls[0][0] == "https://openapi.okx.com/api/v5/market/history-candles"
    assert calls[0][1] == {"params": {"instId": "BTC-USDT", "bar": "1Dutc", "limit": 300}, "timeout": 20}


@pytest.mark.parametrize("data", [
    {"code": "1", "data": []}, {"code": 0, "data": []},
    {"code": "0", "data": {}}, {"code": "0", "data": [["0"]]},
    {"code": "0", "data": [["0", "10", "12", "9", "nan", "1", "1", "11", "1"]]},
    {"code": "0", "data": [["0", "10", "12", "9", "11", "1", "1", "11", "unknown"]]},
])
def test_okx_bad_daily_responses_fail_closed(monkeypatch: pytest.MonkeyPatch, data: object) -> None:
    _get(monkeypatch, data)
    with pytest.raises(ValueError):
        market.okx_candles()


def test_okx_paper_quote_is_public_real_market(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _get(monkeypatch, {"code": "0", "data": [{
        "instId": "BTC-USDT", "bidPx": "50000", "askPx": "50001", "ts": "1800000000000",
    }]})
    quote = market.okx_quote()
    assert quote.bid == 50000 and quote.ask == 50001 and quote.timestamp_ms == 1800000000000
    assert calls[0][0] == "https://openapi.okx.com/api/v5/market/ticker"
    assert "headers" not in calls[0][1]


def test_okx_quote_wrong_instrument_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _get(monkeypatch, {"code": "0", "data": [{"instId": "BTC-USDT-SWAP"}]})
    with pytest.raises(ValueError, match="identity"):
        market.okx_quote()
